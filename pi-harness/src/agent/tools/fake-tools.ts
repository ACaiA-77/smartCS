/**
 * Phase 0 fake read-only tools.
 *
 * Handoff hard constraint #1: these tools are hard-coded and call NO Python
 * endpoint. They exist to prove the tool_call lifecycle and whitelist wiring.
 * Every returned payload is prefixed with [FAKE] so a fake result can never be
 * mistaken for a real business fact in the spike transcript.
 */

import { defineTool } from "@earendil-works/pi-coding-agent";
import { Type } from "@earendil-works/pi-ai";

export const FAKE_ORDER_QUERY_RESULT = {
  orderId: "FAKE-ORDER-1001",
  status: "已发货",
  carrier: "顺丰速运",
  trackingNo: "SF0000000000000",
  items: [{ sku: "FAKE-SKU-01", title: "示例商品（假数据）", qty: 1, amountYuan: 199.0 }],
  paidAt: "2026-09-28T10:12:00+08:00",
  refundable: true,
} as const;

export const FAKE_KNOWLEDGE_SEARCH_RESULT = {
  query: "",
  hits: [
    {
      docId: "FAKE-DOC-REFUND-01",
      title: "七天无理由退货政策（假数据）",
      snippet: "自签收次日起七天内，商品不影响二次销售的，可申请无理由退货。",
    },
  ],
} as const;

export const fakeOrderQueryTool = defineTool({
  name: "order_query",
  label: "订单查询（Phase 0 假工具）",
  description:
    "查询订单状态、物流与商品明细（只读）。Phase 0 假实现：返回硬编码数据，不访问任何业务系统。",
  parameters: Type.Object({
    orderId: Type.String({ description: "订单号" }),
  }),
  async execute(_toolCallId, params) {
    return {
      content: [
        {
          type: "text" as const,
          text: `[FAKE] 订单 ${params.orderId} 查询结果（硬编码演示数据，非真实业务数据）：${JSON.stringify(
            { ...FAKE_ORDER_QUERY_RESULT, orderId: params.orderId },
          )}`,
        },
      ],
      details: { fake: true, tool: "order_query", orderId: params.orderId },
    };
  },
});

export const fakeKnowledgeSearchTool = defineTool({
  name: "knowledge_search",
  label: "知识检索（Phase 0 假工具）",
  description:
    "检索客服政策与帮助文档（只读）。Phase 0 假实现：返回硬编码片段，不访问 RAG 索引。",
  parameters: Type.Object({
    query: Type.String({ description: "检索问题" }),
  }),
  async execute(_toolCallId, params) {
    return {
      content: [
        {
          type: "text" as const,
          text: `[FAKE] 知识检索「${params.query}」命中（硬编码演示数据，非真实 RAG 结果）：${JSON.stringify(
            { ...FAKE_KNOWLEDGE_SEARCH_RESULT, query: params.query },
          )}`,
        },
      ],
      details: { fake: true, tool: "knowledge_search", query: params.query },
    };
  },
});

/** Names of every tool the Phase 0 harness is allowed to activate. */
export const SMARTCS_TOOL_WHITELIST = ["order_query", "knowledge_search"] as const;

/** Names that must never be active in the server-side agent (plan v2 §8 #1). */
export const FORBIDDEN_BUILTIN_TOOLS = ["read", "bash", "edit", "write", "grep", "find", "ls"] as const;

export function createFakeTools() {
  return [fakeOrderQueryTool, fakeKnowledgeSearchTool];
}
