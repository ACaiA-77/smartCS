/**
 * Business READ tool shells (Phase 2).
 *
 * Transport only: each shell posts to the Business Runtime's
 * `/internal/tools/execute` and hands the answer back. No retries, no business
 * rules here — retry, timeout, ledger and authorization all live in Python
 * (plan v2 §7). The `AbortSignal` is forwarded so an aborted SSE stream tears
 * the tool HTTP request down too.
 *
 * Parameter schemas are a field-by-field TypeBox translation of the existing
 * definitions in `python-impl/mcp/mcp_server.py`, which stays the single
 * authority. `user_id` IS translated because the Python definitions declare it;
 * the runtime strips and re-binds it from the verified claims, so a value the
 * model supplies is inert (there is a dedicated acceptance case for that).
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
    // mcp_server.py: order_query
    shell(deps, {
      name: "order_query",
      label: "订单查询",
      description: "查询订单信息，支持按订单号或用户ID查询",
      // required: ["order_id"] in mcp_server.py — user_id is declared but
      // optional there, so it must be optional here too.
      parameters: Type.Object(
        {
          order_id: Type.String({ description: "订单号" }),
          user_id: Type.Optional(Type.String({ description: "用户ID" })),
        },
        { additionalProperties: false },
      ),
    }),
    // mcp_server.py: refund_evaluate
    shell(deps, {
      name: "refund_evaluate",
      label: "退款条件评估",
      description: "评估订单是否符合退款条件",
      parameters: Type.Object(
        {
          order_id: Type.String({ description: "订单号" }),
          user_id: Type.String({ description: "用户ID" }),
        },
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
    // mcp_server.py: ticket_query
    shell(deps, {
      name: "ticket_query",
      label: "工单查询",
      description: "按工单号查询本人客服工单",
      parameters: Type.Object(
        { ticket_id: Type.String(), user_id: Type.String() },
        { additionalProperties: false },
      ),
    }),
    // mcp_server.py: risk_check
    shell(deps, {
      name: "risk_check",
      label: "风控检查",
      description: "风控接口 — 检查交易/操作的风险等级",
      parameters: Type.Object(
        { user_id: Type.String(), action: Type.String(), amount: Type.Optional(Type.Number()) },
        { additionalProperties: false },
      ),
    }),
  ];
}
