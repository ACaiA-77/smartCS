/**
 * Shadow write tools (phase4-design.md §2).
 *
 * In `shadow` mode the model can *decide* to write; the harness records the
 * decision and returns a canned acknowledgement. Nothing is executed and — the
 * load-bearing property — **no internal HTTP request is made**, so shadow mode
 * cannot produce a side effect even if the Python side were misconfigured.
 *
 * Schemas follow the Phase 5 target surface:
 *   * `ticket_create` is a field-for-field translation of the existing
 *     definition in `python-impl/mcp/mcp_server.py`;
 *   * `refund_confirm` does NOT exist in `mcp_server.py` (it is a Phase 5
 *     addition), so its schema is taken from phase4-design.md §2.1.
 * Neither takes a `confirmed` parameter: authorization is never a model input.
 */

import type { AgentToolResult, ToolDefinition } from "@earendil-works/pi-coding-agent";
import { defineTool } from "@earendil-works/pi-coding-agent";
import { Type } from "@earendil-works/pi-ai";
import type { ShadowPlanStore } from "../../db/shadow-plans.js";
import type { ReceiptStore } from "../../db/receipts.js";
import type { PythonInternalClient } from "../../business/python-client.js";
import type { TurnContext, TurnIdentity } from "../../business/turn-context.js";
import { hitCrashPoint } from "../../test-support/crash-point.js";
import type { WriteMode } from "../write-mode.js";

export const SHADOW_WRITE_TOOL_NAMES = ["refund_confirm", "ticket_create"] as const;
export type ShadowWriteToolName = (typeof SHADOW_WRITE_TOOL_NAMES)[number];

export class WriteToolNotPermittedError extends Error {
  constructor(tool: string) {
    super(`write tool "${tool}" is not available in this write mode`);
    this.name = "WriteToolNotPermittedError";
  }
}

export interface ShadowWriteDeps {
  mode: WriteMode;
  /** Required in shadow mode; live mode never records a plan (nothing is simulated). */
  store?: ShadowPlanStore;
  turnContext: TurnContext;
  /** Required in live mode: the runtime client and the durable operation log. */
  client?: PythonInternalClient;
  receipts?: ReceiptStore;
  /** Current turn counter, for the plan record. */
  turnIndex?: () => number;
  /** Last user message, for the audit excerpt. */
  lastUserMessage?: () => string | undefined;
}

export const SHADOW_CANNED_RESULT: Record<ShadowWriteToolName, string> = {
  refund_confirm: "[SHADOW] 退款确认已记录为计划，未执行",
  ticket_create: "[SHADOW] 工单创建已记录为计划，未执行",
};

async function intercept(
  deps: ShadowWriteDeps,
  toolName: ShadowWriteToolName,
  params: Record<string, unknown>,
  signal?: AbortSignal,
  toolCallId?: string,
): Promise<AgentToolResult<unknown>> {
  if (deps.mode === "live") return liveExecute(deps, toolName, params, signal, toolCallId);
  if (deps.mode !== "shadow") throw new WriteToolNotPermittedError(toolName);

  if (!deps.store) throw new Error(`shadow mode requires a plan store for ${toolName}`);

  const identity = deps.turnContext.require();
  const planId = await deps.store.record({
    sessionId: identity.sessionId,
    clientRequestId: identity.clientRequestId,
    toolName,
    arguments: params,
    turnIndex: deps.turnIndex?.() ?? 0,
    userMessageExcerpt: deps.lastUserMessage?.() ?? "",
  });

  return {
    content: [{ type: "text" as const, text: SHADOW_CANNED_RESULT[toolName] }],
    details: {
      shadow: true,
      executed: false,
      planId,
      tool: toolName,
      // Program-only metadata; never enters the model's context.
      planArguments: params,
    },
  };
}

export function createShadowWriteTools(deps: ShadowWriteDeps): ToolDefinition[] {
  const refundConfirm = defineTool({
    name: "refund_confirm",
    label: "退款确认（Shadow）",
    description: "在用户明确确认后提交退款申请。只在用户已确认时调用。",
    parameters: Type.Object(
      {
        pending_action_id: Type.String({ description: "退款评估返回的待确认动作 ID" }),
      },
      { additionalProperties: false },
    ),
    async execute(toolCallId, params, signal) {
      return intercept(deps, "refund_confirm", params as Record<string, unknown>, signal, toolCallId);
    },
  }) as unknown as ToolDefinition;

  const ticketCreate = defineTool({
    name: "ticket_create",
    label: "创建工单（Shadow）",
    description: "创建客服工单",
    // Field-for-field translation of mcp_server.py's ticket_create. Note the
    // absence of any `confirmed` parameter.
    parameters: Type.Object(
      {
        client_request_id: Type.String(),
        request_payload_hash: Type.String(),
        user_id: Type.String(),
        title: Type.String(),
        description: Type.String(),
        priority: Type.Optional(Type.String({ enum: ["low", "medium", "high", "urgent"] })),
        category: Type.Optional(Type.String()),
      },
      { additionalProperties: false },
    ),
    async execute(toolCallId, params, signal) {
      return intercept(deps, "ticket_create", params as Record<string, unknown>, signal, toolCallId);
    },
  }) as unknown as ToolDefinition;

  return [refundConfirm, ticketCreate];
}

/**
 * Deterministic placeholder id for the two-phase flow's first half.
 *
 * Phase 5 will allocate a real `pending_action_id` in MySQL; shadow mode must
 * not write anything, so the id is derived from the verified identity plus the
 * order, which makes it stable across runs and therefore comparable.
 */
export function shadowPendingActionId(parts: {
  sessionId: string;
  clientRequestId: string;
  orderId: string;
}): string {
  const input = `${parts.sessionId}|${parts.clientRequestId}|${parts.orderId}`;
  let hash = 0;
  for (let index = 0; index < input.length; index += 1) {
    hash = (hash * 31 + input.charCodeAt(index)) >>> 0;
  }
  return `pending-shadow-${hash.toString(16).padStart(8, "0")}`;
}

// --- live mode (Phase 5d) ---------------------------------------------------

/**
 * Execute a real write through the runtime, then make the OUTCOME authoritative.
 *
 * The dangerous case is a transport failure: the write may or may not have
 * happened. The harness never guesses there — it finds the operation id from
 * the durable receipt record (written by the runtime BEFORE sending) and asks
 * the ledger. A blind retry would be the one thing that can duplicate a refund,
 * so `UNKNOWN` is surfaced as an error, never as "try again".
 */
async function liveExecute(
  deps: ShadowWriteDeps,
  toolName: ShadowWriteToolName,
  params: Record<string, unknown>,
  signal?: AbortSignal,
  toolCallId?: string,
): Promise<AgentToolResult<unknown>> {
  if (!deps.client || !deps.receipts) {
    throw new Error(`live write mode is not fully wired for ${toolName}`);
  }
  const identity = deps.turnContext.require();

  // F3 window: the model decided to write, nothing has left this process. The
  // runtime has not reserved an operation, so a resend is safe by definition.
  hitCrashPoint("before_write_send");

  try {
    const response = await deps.client.executeTool({
      tool: toolName,
      arguments: params,
      identity,
      toolCallId,
      signal,
    });
    const details = (response.details ?? {}) as Record<string, unknown>;
    // F5 window: the runtime reports the side effect as executed, so it IS
    // durable — but Pi has not appended the toolResult. This is the one place
    // where "it happened but the transcript does not know" is true.
    if (details.authorized === true && details.executed === true) hitCrashPoint("after_write_success");
    if (details.authorized === false) {
      // A business refusal is a normal result the model must see.
      return { content: [{ type: "text" as const, text: response.content }], details };
    }
    return { content: [{ type: "text" as const, text: response.content }], details };
  } catch (error) {
    return reconcileAfterTransportFailure(deps, toolName, identity, error);
  }
}

async function reconcileAfterTransportFailure(
  deps: ShadowWriteDeps,
  toolName: ShadowWriteToolName,
  identity: TurnIdentity,
  error: unknown,
): Promise<AgentToolResult<unknown>> {
  const operations = await deps.receipts!.openWriteOperations(identity.sessionId, identity.clientRequestId);
  const operation = [...operations].reverse().find((item) => item.tool === toolName);

  if (!operation) {
    // No durable record ⇒ the write was never sent. Failing is correct and
    // safe: the user can simply retry.
    throw new Error(
      `工具 ${toolName} 未能提交（未发出任何写操作）：${error instanceof Error ? error.message : String(error)}`,
    );
  }

  const verdict = await deps.client!.operationStatus({ identity, operationId: operation.operation_id });

  if (verdict.status === "COMPLETED") {
    // Deterministic recovery (design §4 / F5): the side effect DID happen, so
    // finish the request from the authority instead of replaying anything.
    return {
      content: [
        {
          type: "text" as const,
          text: `[业务已完成] ${toolName} 已由业务系统执行完成（恢复自账簿，未重复执行）。`,
        },
      ],
      details: {
        recovered: true,
        executed: true,
        operationId: operation.operation_id,
        ledgerStatus: verdict.status,
        result: verdict.result,
      },
    };
  }

  if (verdict.status === "FAILED") {
    throw new Error(`工具 ${toolName} 执行失败（账簿权威结论），未产生任何业务变更。`);
  }

  // UNKNOWN or PROVABLY_NOT_EXECUTED: do not act automatically. Note that
  // PROVABLY_NOT_EXECUTED is safe to retry by a human/orchestrator, but this
  // shell deliberately does not retry — retry policy is not a transport concern.
  throw new Error(
    `工具 ${toolName} 结果未确定（${verdict.status}），已停止自动处理，需人工核对后再决定。`,
  );
}
