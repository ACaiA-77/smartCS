/**
 * The Phase 1 chat pipeline (phase1-design.md §5).
 *
 *   1 local user-JWT verify                    (caller-supplied accountId)
 *   2 POST /internal/auth/verify               identity + ownership + routing
 *   3 registry.acquire(session)                same-session serialisation
 *   4 receipt lookup                           replay / 409 / run
 *   5 INSERT memory_source_event               durable provenance BEFORE any LLM work
 *   6 先查后建 Pi session
 *   7 prompt
 *   8 final (buffered)
 *   9 receipt -> completed
 *  10 respond
 *
 * Crash window semantics: before step 5 nothing is persisted (a re-send is
 * harmless); between 5 and 9 the receipt stays `processing` and the next
 * request reclaims it as an orphan. Phase 1 has no WRITE tools, so "rerun from
 * the start" cannot duplicate a side effect — that reasoning expires in Phase 5.
 *
 * NOTE on the service token: at step 2 the harness does NOT yet know
 * `business_user_id` (that is what it is asking Python to resolve), so the
 * service token binds `account_id` + `session_id` only. Plan v2 §7 lists
 * business_user_id among the claims; it becomes mandatory again from Phase 2
 * where tool calls really do need to carry the resolved identity.
 */

import { randomUUID } from "node:crypto";
import type { StreamFrame } from "../streaming/status.js";
import { SAFE_FALLBACK_FINAL } from "../streaming/status.js";
import { ChatStream } from "../streaming/chat-stream.js";
import type { MemorySourceStore } from "../db/memory-source.js";
import { hashRequest, type ReceiptStore } from "../db/receipts.js";
import type { PythonInternalClient } from "../business/python-client.js";
import { PythonInternalError } from "../business/python-client.js";
import type { SessionRegistry } from "../session/registry.js";
import { SessionBusyError, SessionQueueFullError } from "../session/registry.js";
import { hitCrashPoint } from "../test-support/crash-point.js";
import { ATTR, startTurnSpan, type TurnSpan } from "../tracing/spans.js";
import { HttpError } from "./http-error.js";

export interface ChatInput {
  userJwt: string;
  /** From the locally verified user token; Python re-derives it authoritatively. */
  accountId: number;
  sessionId: string;
  clientRequestId: string;
  message: string;
  onFrame?: (frame: StreamFrame) => void;
  leaseTimeoutMs?: number;
  /**
   * Phase 6: an upstream `traceparent` header, honoured when it is well formed
   * so a gateway can start the trace. Otherwise the harness mints the trace id.
   */
  traceparent?: string;
}

export interface ChatSuccess {
  kind: "ok";
  sessionId: string;
  clientRequestId: string;
  message: { role: "assistant"; content: string };
  replayed: boolean;
  meta: {
    reclaimedFrom: string;
    piOrigin: string;
    settledCount: number;
    frames: StreamFrame[];
    intentLabel: string;
    /** Compaction events observed during the run (Phase 3 §5, observation only). */
    compactionEvents: string[];
  };
}

export interface ChatPipelineDeps {
  registry: SessionRegistry;
  receipts: ReceiptStore;
  memorySource: MemorySourceStore;
  pythonClient: PythonInternalClient;
  idleEvictionMs?: number;
  /** Override the observation-only intent labeller (tests, future LLM version). */
  classifyIntent?: (message: string) => string;
}

const MAX_MESSAGE_CHARS = 20_000;

function assertIdentifier(value: unknown, name: string, max = 128): string {
  if (typeof value !== "string" || value.length === 0 || value.length > max || value.includes("\u0000")) {
    throw new HttpError(400, `invalid ${name}`);
  }
  return value;
}

const storedResponse = (
  content: string,
  intentLabel: string,
  compactionEvents: string[],
  traceId?: string,
) => ({
  message: { role: "assistant" as const, content },
  // Observation-only metadata (design §3/§5). Nothing reads it back to decide
  // behaviour — it exists for UI/eval/metrics.
  metadata: {
    intent_label: intentLabel,
    compaction_events: compactionEvents,
    // Phase 6 (plan v2 §6.9): the turn's trace id is durable with the answer,
    // so a replay can still be correlated with the original run.
    ...(traceId ? { trace_id: traceId } : {}),
  },
});

/**
 * Lightweight, deterministic intent labelling (design §3).
 *
 * This is NOT routing. Main-agent tool choice decides what happens; this label
 * is written next to the response for observability only. The regex version is
 * deliberately the Phase 2 choice; an LLM-based version is a Phase 3 question.
 */
export function classifyIntent(message: string): string {
  const text = message.toLowerCase();
  if (/(退款|退钱|退货|refund)/.test(text)) return "refund";
  if (/(工单|投诉|举报|ticket|升级)/.test(text)) return "ticket";
  if (/(订单|物流|快递|发货|order|shipping)/.test(text)) return "order";
  if (/(风险|风控|risk|异常交易)/.test(text)) return "risk";
  if (/(政策|规则|怎么|如何|什么是|流程|policy|how)/.test(text)) return "knowledge";
  return "general";
}

function contentOf(response: unknown): string | undefined {
  const value = (response as { message?: { content?: unknown } } | null)?.message?.content;
  return typeof value === "string" ? value : undefined;
}

/**
 * One chat request. Phase 6 wraps the whole turn — including the paths that
 * never reach the model (replay, ledger recovery) — in a single trace context,
 * so every internal call the request makes is attributable to one trace.
 */
export async function runChat(deps: ChatPipelineDeps, input: ChatInput): Promise<ChatSuccess> {
  const sessionId = assertIdentifier(input.sessionId, "session_id");
  const clientRequestId = assertIdentifier(input.clientRequestId, "client_request_id");
  const turn = startTurnSpan({ traceparent: input.traceparent, sessionId, clientRequestId });
  try {
    const result = await runTurn(deps, input, sessionId, clientRequestId, turn);
    turn.setAttribute(ATTR.outcome, "ok");
    return result;
  } catch (error) {
    turn.setAttribute(ATTR.outcome, "error");
    throw error;
  } finally {
    // Synchronous: the SDK's processor owns the export. No await on this path.
    turn.end();
  }
}

async function runTurn(
  deps: ChatPipelineDeps,
  input: ChatInput,
  sessionId: string,
  clientRequestId: string,
  turn: TurnSpan,
): Promise<ChatSuccess> {
  if (typeof input.message !== "string" || input.message.length === 0) {
    throw new HttpError(400, "invalid message");
  }
  if (input.message.length > MAX_MESSAGE_CHARS) throw new HttpError(413, "message too large");
  if (!Number.isInteger(input.accountId) || input.accountId <= 0) {
    throw new HttpError(401, "invalid authentication");
  }

  // (2) authoritative identity — the harness asserts nothing itself.
  let identity;
  try {
    identity = await deps.pythonClient.verifyIdentity({
      userJwt: input.userJwt,
      sessionId,
      accountId: input.accountId,
      clientRequestId,
      traceparent: turn.traceparent,
    });
  } catch (error) {
    if (error instanceof PythonInternalError) throw new HttpError(error.status, error.detail);
    throw error;
  }
  if (identity.harness_version !== "pi") {
    throw new HttpError(409, "session is not served by the pi harness");
  }

  // (3) single writer per session.
  let lease;
  try {
    lease = await deps.registry.acquire(sessionId, input.leaseTimeoutMs);
  } catch (error) {
    if (error instanceof SessionBusyError) throw new HttpError(429, "session busy");
    if (error instanceof SessionQueueFullError) throw new HttpError(429, "session queue full");
    throw error;
  }

  try {
    // (4) receipt decision.
    const decision = await deps.receipts.beginRequest(sessionId, clientRequestId, hashRequest(input.message));
    if (decision.action === "conflict") throw new HttpError(409, decision.detail);

    // The receipt row id IS the agent_run_id, and it exists from here on.
    turn.setAgentRunId(String(decision.receiptId));

    if (decision.action === "replay") {
      const content = contentOf(decision.response) ?? SAFE_FALLBACK_FINAL;
      turn.setAttribute(ATTR.replayed, true);
      input.onFrame?.({ type: "final", text: content, at: Date.now() });
      input.onFrame?.({ type: "done", at: Date.now(), reason: "settled" });
      return {
        kind: "ok",
        sessionId,
        clientRequestId,
        message: { role: "assistant", content },
        replayed: true,
        meta: {
          reclaimedFrom: "none",
          piOrigin: "replayed",
          settledCount: 0,
          frames: [],
          intentLabel: storedIntentLabel(decision.response),
          compactionEvents: [],
        },
      };
    }

    // (4b) Phase 5F: a receipt that is being RUN may already carry writes that
    // happened on a previous attempt. The receipt log is durable before the
    // write is sent, so it is the only trustworthy list of candidates; the
    // ledger is the only trustworthy verdict. Reconcile FIRST — rerunning the
    // model over a completed write is exactly the duplication F5/F13 forbid.
    const ledgerRecovery = await reconcileOpenWrites(
      deps,
      input,
      identity,
      sessionId,
      clientRequestId,
      decision.receiptId,
      turn,
    );
    if (ledgerRecovery) {
      turn.setAttribute(ATTR.recoveredFrom, "ledger");
      return ledgerRecovery;
    }

    // (5) durable provenance before any model activity.
    await deps.memorySource.record({
      eventId: randomUUID(),
      sessionId,
      businessUserId: identity.business_user_id,
      clientRequestId,
      content: input.message,
    });

    // (6) Prefetch the turn snapshot BEFORE the model runs. It cannot be
    // parallel with identity resolution because the service token needs the
    // resolved business_user_id — but it is still off the pi.on path, which is
    // what plan v2 §6.6 actually forbids.
    const turnIdentity = {
      accountId: identity.account_id,
      businessUserId: identity.business_user_id,
      sessionId,
      clientRequestId,
      traceparent: turn.traceparent,
      agentRunId: String(decision.receiptId),
    };
    let snapshot: Awaited<ReturnType<PythonInternalClient["fetchTurnSnapshot"]>> | undefined;
    try {
      snapshot = await deps.pythonClient.fetchTurnSnapshot({ identity: turnIdentity });
    } catch {
      // A missing snapshot degrades context, it does not fail the turn.
      snapshot = undefined;
    }

    try {
      // (7) 先查后建, inside the lease.
      const { session, origin, turnContext, snapshotHolder } = await deps.registry.ensureSession(sessionId);

      // (8) The tool shells read identity from here, and the injection
      // extension reads the snapshot. Safe because the lease guarantees one
      // in-flight request per session.
      turnContext?.set(turnIdentity);
      if (snapshot) snapshotHolder?.set(snapshot);
      let result;
      try {
        // F1 window: everything durable is in place (receipt processing,
        // provenance row), the model has not been asked anything yet.
        hitCrashPoint("before_llm_call");
        const stream = new ChatStream(session, { onFrame: input.onFrame, spans: turn });
        result = await stream.run(input.message);
      } finally {
        turnContext?.clear();
        snapshotHolder?.clear();
      }

      // F6 window: `final` exists (and any WRITE already executed), but the
      // receipt is still `processing`, so a reader cannot yet see the outcome.
      hitCrashPoint("before_receipt_complete");

      // (9) The intent label is observation-only: computed after the run and
      // stored with the response, never consulted by any execution path.
      const intentLabel = deps.classifyIntent?.(input.message) ?? classifyIntent(input.message);
      turn.setAttribute(ATTR.intentLabel, intentLabel);
      turn.setAttribute("smartcs.settled_count", result.settledCount);
      const stored = await deps.receipts.complete(
        decision.receiptId,
        storedResponse(result.finalText, intentLabel, result.compactionEvents, turn.traceId),
      );
      if (!stored) throw new HttpError(409, "receipt state changed during the run");

      return {
        kind: "ok",
        sessionId,
        clientRequestId,
        message: { role: "assistant", content: result.finalText },
        replayed: false,
        meta: {
          reclaimedFrom: decision.reclaimedFrom,
          piOrigin: origin,
          settledCount: result.settledCount,
          frames: result.frames,
          intentLabel,
          compactionEvents: result.compactionEvents,
        },
      };
    } catch (error) {
      await deps.receipts.failRecoverable(
        decision.receiptId,
        error instanceof Error ? error.message : "run failed",
      );
      throw error;
    } finally {
      deps.registry.scheduleIdleEviction(sessionId, deps.idleEvictionMs ?? 15 * 60_000);
    }
  } finally {
    lease.release();
  }
}

/**
 * Settle a re-run request from the write ledger instead of from a fresh model
 * turn (design §4: "有 write op → 先 reconcile，禁盲跑").
 *
 * Three outcomes, and the asymmetry is the point:
 *   * COMPLETED — the side effect exists. Finish the request with a
 *     deterministic "business already done" answer and complete the receipt.
 *     The model is NOT consulted, so it cannot decide to do it again.
 *   * UNKNOWN — nothing can be concluded. Refuse the request; a human resolves
 *     it. Never a blind rerun.
 *   * FAILED / PROVABLY_NOT_EXECUTED (or no operations at all) — nothing
 *     happened, so returning undefined lets the normal run proceed.
 */
async function reconcileOpenWrites(
  deps: ChatPipelineDeps,
  input: ChatInput,
  identity: { account_id: number; business_user_id: string },
  sessionId: string,
  clientRequestId: string,
  receiptId: number,
  turn: TurnSpan,
): Promise<ChatSuccess | undefined> {
  const operations = await deps.receipts.openWriteOperations(sessionId, clientRequestId);
  if (operations.length === 0) return undefined;

  const turnIdentity = {
    accountId: identity.account_id,
    businessUserId: identity.business_user_id,
    sessionId,
    clientRequestId,
    traceparent: turn.traceparent,
    agentRunId: String(receiptId),
  };
  const verdicts: Array<{ operation_id: string; tool: string; status: string }> = [];
  for (const operation of operations) {
    const verdict = await deps.pythonClient.operationStatus({
      identity: turnIdentity,
      operationId: operation.operation_id,
    });
    verdicts.push({ operation_id: operation.operation_id, tool: operation.tool, status: verdict.status });
  }

  if (verdicts.some((item) => item.status === "UNKNOWN")) {
    throw new HttpError(
      409,
      "写操作结果未确定（账簿未能给出结论），已停止自动处理，需人工核对后再决定",
    );
  }

  const completed = verdicts.filter((item) => item.status === "COMPLETED");
  if (completed.length === 0) return undefined; // provably nothing happened → safe to run

  const tools = [...new Set(completed.map((item) => item.tool))].join("、");
  const content = `[业务已完成] ${tools} 已由业务系统执行完成（恢复自账簿，未重复执行）。`;
  const intentLabel = deps.classifyIntent?.(input.message) ?? classifyIntent(input.message);
  const stored = await deps.receipts.complete(receiptId, storedResponse(content, intentLabel, [], turn.traceId));
  if (!stored) throw new HttpError(409, "receipt state changed during recovery");

  input.onFrame?.({ type: "final", text: content, at: Date.now() });
  input.onFrame?.({ type: "done", at: Date.now(), reason: "settled" });

  return {
    kind: "ok",
    sessionId,
    clientRequestId,
    message: { role: "assistant", content },
    replayed: false,
    meta: {
      reclaimedFrom: "ledger_recovery",
      piOrigin: "ledger_recovery",
      settledCount: 0,
      frames: [],
      intentLabel,
      compactionEvents: [],
    },
  };
}

/** Read back the observation label stored with a replayed response. */
function storedIntentLabel(response: unknown): string {
  const value = (response as { metadata?: { intent_label?: unknown } } | null)?.metadata?.intent_label;
  return typeof value === "string" ? value : "unknown";
}
