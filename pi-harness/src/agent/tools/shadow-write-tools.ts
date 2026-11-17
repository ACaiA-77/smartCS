/**
 * Shadow write tools (phase4-design.md §2).
 *
 * In `shadow` mode the model can *decide* to write; the harness records the
 * decision and returns a canned acknowledgement. Nothing is executed and — the
 * load-bearing property — **no internal HTTP request is made**, so shadow mode
 * cannot produce a side effect even if the Python side were misconfigured.
 *
 * Schemas follow the Phase 5 target surface, narrowed by the Phase 10 §③ rule
 * (see `business-tools.ts` for the full rationale): the model sees business
 * parameters only.
 *   * `ticket_create` declares title/description/priority?/category? — the
 *     runtime supplies `user_id`, `client_request_id` and `request_payload_hash`
 *     itself at the live-write boundary (`internal_api/tools.py`
 *     `_execute_live_write`), so declaring them here would be three more fields
 *     for the model to get wrong for no effect;
 *   * `refund_confirm` does NOT exist in `mcp_server.py` (it is a Phase 5
 *     addition), so its schema is taken from phase4-design.md §2.1. It takes
 *     only the pending id — the business parameters come from the frozen
 *     pending snapshot on the Python side.
 * Neither takes a `confirmed` parameter: authorization is never a model input.
 */

import type { AgentToolResult, ToolDefinition } from "@earendil-works/pi-coding-agent";
import { defineTool } from "@earendil-works/pi-coding-agent";
import { Type } from "@earendil-works/pi-ai";
import type { ShadowPlanStore } from "../../db/shadow-plans.js";
import type { ReceiptStore } from "../../db/receipts.js";
import type { PythonInternalClient } from "../../business/python-client.js";
import type { TurnContext } from "../../business/turn-context.js";
import type { WriteMode } from "../write-mode.js";
import { executeLiveWrite } from "./live-write-executor.js";

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
  if (deps.mode === "live") {
    if (!deps.client || !deps.receipts) {
      throw new Error(`live write mode is not fully wired for ${toolName}`);
    }
    return executeLiveWrite(
      { client: deps.client, receipts: deps.receipts },
      deps.turnContext.require(),
      toolName,
      params,
      signal,
      toolCallId,
    );
  }
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
    // Business parameters only; no `confirmed`, no identity, no idempotency
    // key. Names match mcp_server.py's ticket_create handler.
    parameters: Type.Object(
      {
        title: Type.String({ description: "工单标题" }),
        description: Type.String({ description: "问题描述" }),
        priority: Type.Optional(
          Type.String({ enum: ["low", "medium", "high", "urgent"], description: "优先级" }),
        ),
        category: Type.Optional(Type.String({ description: "工单分类" })),
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
