/**
 * Audit extension.
 *
 * Plan v2 §6.9: audit hooks run through `pi.on("tool_call"/"tool_result")`
 * because those two events are EXTENSION-ONLY — `session.subscribe` never sees
 * them (plan v2 §8 red line #7).
 *
 * The extension handler IS awaited by the dispatcher, so the handler itself
 * must stay fast. Phase 6 keeps that property and adds durability: everything
 * here is a synchronous object build plus an in-memory push — the bounded
 * queue is drained later by a background dispatcher, never by the hook.
 */

import type { ExtensionAPI, ExtensionFactory } from "@earendil-works/pi-coding-agent";
import type { TurnContext, TurnIdentity } from "../../business/turn-context.js";
import { newAuditEvent, type AuditQueue } from "../../tracing/audit-queue.js";
import { parseTraceparent } from "../../tracing/trace-context.js";

export interface AuditRecord {
  kind: "tool_call" | "tool_result";
  toolName: string;
  toolCallId?: string;
  at: number;
  /** Present for tool_call only. */
  input?: unknown;
  /** Present for tool_result only. */
  isError?: boolean;
  // --- Phase 6 (all optional; the pre-Phase-6 shape still reads fine) ---
  /** Idempotency key of the durable row this record becomes. */
  eventId?: string;
  /** Runtime write-ledger operation, when the tool produced one. */
  operationId?: string;
  traceId?: string;
  sessionId?: string;
  clientRequestId?: string;
}

export interface AuditSink {
  records: AuditRecord[];
  /** Registered by the harness to drain asynchronously in production. */
  drain: () => AuditRecord[];
  /**
   * Phase 6: when present, every record is ALSO pushed here for durable
   * delivery. The hook still only pushes — it never awaits the dispatcher.
   */
  queue?: AuditQueue;
  /** Times the in-process list hit its cap (bounded, never unbounded growth). */
  noteOverflow: () => void;
  overflowed: () => number;
}

/** Cap for the in-process diagnostic list (`records`). */
export const AUDIT_RECORD_CAP = 1_000;

export function createAuditSink(options: { queue?: AuditQueue; capacity?: number } = {}): AuditSink {
  const records: AuditRecord[] = [];
  let overflowed = 0;
  return {
    records,
    drain: () => records.splice(0, records.length),
    queue: options.queue,
    noteOverflow: () => {
      overflowed += 1;
    },
    overflowed: () => overflowed,
  };
}

export function createAuditExtension(sink: AuditSink, turnContext?: TurnContext): ExtensionFactory {
  /** The identity of the request this tool call belongs to, if any. */
  const identityOf = (): TurnIdentity | undefined => turnContext?.peek();

  const remember = (record: AuditRecord): void => {
    const identity = identityOf();
    const traceId = parseTraceparent(identity?.traceparent)?.traceId;
    const enriched: AuditRecord = {
      ...record,
      ...(traceId ? { traceId } : {}),
      ...(identity ? { sessionId: identity.sessionId, clientRequestId: identity.clientRequestId } : {}),
    };

    if (sink.records.length >= AUDIT_RECORD_CAP) {
      sink.records.shift();
      sink.noteOverflow();
    }
    sink.records.push(enriched);

    if (sink.queue && identity) {
      const event = newAuditEvent({
        kind: enriched.kind,
        toolName: enriched.toolName,
        toolCallId: enriched.toolCallId,
        operationId: enriched.operationId,
        traceId,
        payload: enriched.kind === "tool_call" ? enriched.input : { isError: enriched.isError },
      });
      sink.queue.push(event, {
        accountId: identity.accountId,
        businessUserId: identity.businessUserId,
        sessionId: identity.sessionId,
        clientRequestId: identity.clientRequestId,
        ...(identity.traceparent ? { traceparent: identity.traceparent } : {}),
      });
    }
  };

  return (pi: ExtensionAPI) => {
    pi.on("tool_call", (event) => {
      // Fire-and-forget: no await, no network, no MySQL. See plan v2 §6.9.
      remember({
        kind: "tool_call",
        toolName: event.toolName,
        toolCallId: (event as { toolCallId?: string }).toolCallId,
        at: Date.now(),
        input: (event as { input?: unknown }).input,
      });
      return undefined;
    });

    pi.on("tool_result", (event) => {
      // The write tools report their ledger operation id in `details`; it is
      // one of the six propagated ids, so it is worth carrying into the row.
      const details = (event as { details?: unknown }).details;
      let operationId: string | undefined;
      if (details && typeof details === "object") {
        const value =
          (details as { operationId?: unknown }).operationId ?? (details as { operation_id?: unknown }).operation_id;
        if (typeof value === "string" && value.length > 0) operationId = value;
      }
      remember({
        kind: "tool_result",
        toolName: event.toolName,
        toolCallId: (event as { toolCallId?: string }).toolCallId,
        at: Date.now(),
        isError: (event as { isError?: boolean }).isError,
        ...(operationId ? { operationId } : {}),
      });
      return undefined;
    });
  };
}
