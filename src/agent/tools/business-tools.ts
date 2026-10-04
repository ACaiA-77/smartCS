/**
 * Business READ tool shells (Phase 2).
 *
 * Transport only: each shell posts to the Business Runtime's
 * `/internal/tools/execute` and hands the answer back. No retries, no business
 * rules here — retry, timeout, ledger and authorization all live in Python
 * (plan v2 §7). The `AbortSignal` is forwarded so an aborted SSE stream tears
 * the tool HTTP request down too.
 *
 * ── Schema ownership (Phase 10 §③ — explicit reversal of the Phase 2 rule) ──
 *
 * Phase 2 translated `python-impl/mcp/mcp_server.py` field-for-field, including
 * `user_id`, on the grounds that the Python definition is the single authority.
 * That is reversed here, deliberately:
 *
 *   THE MODEL-VISIBLE SCHEMA IS THE BUSINESS-PARAMETER SUBSET.
 *
 * Identity, authorization, idempotency and tracing parameters belong to the
 * runtime, not to the model. `user_id` is stripped from `arguments` and
 * force-bound from the verified service claims (`internal_api/tools.py` §4/§4b);
 * `client_request_id` and `request_payload_hash` are supplied server-side at
 * the live-write boundary (§_execute_live_write). A model that supplies them
 * changes nothing — the values are dropped — so declaring them only adds
 * schema noise, more ways to omit a required field, and a misleading picture of
 * who is responsible for what. The Python side is UNCHANGED: it still declares
 * and enforces the full schema, and still refuses a call that arrives without
 * the injected fields.
 *
 * Consequence for readers comparing the two files: this list is intentionally
 * NOT a translation of `input_schema`. The Python definition remains the
 * authority for what the runtime accepts; these schemas are the authority for
 * what the model is asked to decide. The mapping is asserted end-to-end by the
 * Phase 10 acceptance cases, which drive the real runtime with these schemas.
 *
 * Parameter names are still taken verbatim from the Python handlers — a name
 * the handler does not accept is a call that fails at `handler(**arguments)`.
 */

import type { AgentToolResult, ToolDefinition } from "@earendil-works/pi-coding-agent";
import { defineTool } from "@earendil-works/pi-coding-agent";
import { Type } from "@earendil-works/pi-ai";
import { PythonInternalError, type PythonInternalClient } from "../../business/python-client.js";
import type { TurnContext } from "../../business/turn-context.js";

export const READ_TOOL_NAMES = [
  "knowledge_search",
  "order_query",
  "ticket_query",
  "refund_evaluate",
  "risk_check",
] as const;

export type ReadToolName = (typeof READ_TOOL_NAMES)[number];

export interface BusinessToolDeps {
  client: PythonInternalClient;
  turnContext: TurnContext;
  /**
   * Shadow mode (Phase 4): `refund_evaluate` additionally reports the
   * placeholder `pending_action_id` that the two-phase flow's second half would
   * consume. Returning undefined leaves the read result untouched.
   */
  shadowPendingActionFor?: (params: Record<string, unknown>) => string | undefined;
}

/**
 * A tool failure becomes a thrown Error, which the SDK turns into an
 * `isError: true` tool result the model can read and react to. The message is
 * the runtime's own wording — never a raw stack or transport detail.
 */
function toToolError(error: unknown, tool: string): Error {
  if (error instanceof PythonInternalError) {
    return new Error(`工具 ${tool} 调用失败（${error.status}）：${error.detail}`);
  }
  return new Error(`工具 ${tool} 调用失败：${error instanceof Error ? error.message : String(error)}`);
}

function shell(
  deps: BusinessToolDeps,
  definition: {
    name: ReadToolName;
    label: string;
    description: string;
    parameters: ReturnType<typeof Type.Object>;
  },
): ToolDefinition {
  return defineTool({
    name: definition.name,
    label: definition.label,
    description: definition.description,
    parameters: definition.parameters,
    async execute(toolCallId, params, signal): Promise<AgentToolResult<unknown>> {
      const identity = deps.turnContext.require();
      let response;
      try {
        response = await deps.client.executeTool({
          tool: definition.name,
          arguments: params as Record<string, unknown>,
          identity,
          toolCallId,
          signal,
        });
      } catch (error) {
        throw toToolError(error, definition.name);
      }
      const pendingActionId =
        definition.name === "refund_evaluate"
          ? deps.shadowPendingActionFor?.(params as Record<string, unknown>)
          : undefined;
      if (!pendingActionId) {
        return {
          content: [{ type: "text" as const, text: response.content }],
          details: response.details,
        };
      }
      // Shadow: surface the first half of the two-phase flow so the model can
      // echo it back on the confirmation turn. Nothing is persisted.
      const details =
        response.details && typeof response.details === "object"
          ? { ...(response.details as Record<string, unknown>), pending_action_id: pendingActionId }
          : { pending_action_id: pendingActionId };
      return {
        content: [
          { type: "text" as const, text: `${response.content}
pending_action_id=${pendingActionId}` },
        ],
        details,
      };
    },
  }) as unknown as ToolDefinition;
}

export function createBusinessReadTools(deps: BusinessToolDeps): ToolDefinition[] {
  return [
    // mcp_server.py: order_query. Model params: order_id. `user_id` is
    // force-bound server-side, which is also what scopes the lookup to the
    // caller's own orders.
    shell(deps, {
      name: "order_query",
      label: "订单查询",
      description: "查询订单信息，支持按订单号查询",
      parameters: Type.Object(
        { order_id: Type.String({ description: "订单号" }) },
        { additionalProperties: false },
      ),
    }),
    // mcp_server.py: refund_evaluate. Model params: order_id.
    // The review suggested an optional `reason?` here; the Python handler is
    // `refund_evaluate(order_id, user_id)` and would reject an unknown keyword,
    // so it is deliberately NOT declared (PHASE10_REPORT.md §偏差).
    shell(deps, {
      name: "refund_evaluate",
      label: "退款条件评估",
      description: "评估订单是否符合退款条件",
      parameters: Type.Object(
        { order_id: Type.String({ description: "订单号" }) },
        { additionalProperties: false },
      ),
    }),
    // mcp_server.py: knowledge_search
    shell(deps, {
      name: "knowledge_search",
      label: "知识库检索",
      description: "搜索企业知识库，返回相关文档片段",
      parameters: Type.Object(
        {
          query: Type.String({ description: "搜索查询" }),
          top_k: Type.Optional(Type.Integer({ description: "返回数量", default: 3 })),
          domain: Type.Optional(Type.String({ description: "可选知识域" })),
          domains: Type.Optional(Type.Array(Type.String())),
        },
        { additionalProperties: false },
      ),
    }),
    // mcp_server.py: ticket_query. Model params: ticket_id. `user_id` is
    // force-bound, which is what makes this "本人工单" rather than any ticket.
    shell(deps, {
      name: "ticket_query",
      label: "工单查询",
      description: "按工单号查询本人客服工单",
      parameters: Type.Object(
        { ticket_id: Type.String({ description: "工单号" }) },
        { additionalProperties: false },
      ),
    }),
    // mcp_server.py: risk_check. Model params: action, amount?. The subject of
    // the check is the caller, never a model-chosen user.
    shell(deps, {
      name: "risk_check",
      label: "风控检查",
      description: "风控接口 — 检查交易/操作的风险等级",
      parameters: Type.Object(
        {
          action: Type.String({ description: "待检查的操作" }),
          amount: Type.Optional(Type.Number({ description: "涉及金额" })),
        },
        { additionalProperties: false },
      ),
    }),
  ];
}
