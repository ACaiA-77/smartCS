/**
 * Offline fake read-only tools for SDK-level probes.
 *
 * These tools are hard-coded and call NO Python endpoint. Every returned
 * payload is prefixed with [FAKE] so a fake result can never be mistaken for a
 * real workspace fact.
 */

import { defineTool } from "@earendil-works/pi-coding-agent";
import { Type } from "@earendil-works/pi-ai";

export const FAKE_VAULT_SEARCH_RESULT = {
  query: "",
  hits: [
    {
      path: "notes/roadmap.md",
      title: "Personal Copilot Roadmap（假数据）",
      zone: "notes",
      snippet: "Obsidian LLM Wiki 与本体图谱统一检索工作流。",
    },
  ],
} as const;

export const FAKE_KNOWLEDGE_SEARCH_RESULT = {
  query: "",
  hits: [
    {
      docId: "FAKE-DOC-COPILOT-01",
      title: "Obsidian 知识库使用规范（假数据）",
      snippet: "notes/ 目录只读，修改统一通过 vault_propose_edit 提交提案。",
    },
  ],
} as const;

export const fakeVaultSearchTool = defineTool({
  name: "vault_search",
  label: "笔记检索（离线假工具）",
  description:
    "搜索 Obsidian 笔记（只读）。离线假实现：返回硬编码数据，不访问真实 Vault。",
  parameters: Type.Object({
    query: Type.String({ description: "搜索关键词" }),
  }),
  async execute(_toolCallId, params) {
    return {
      content: [
        {
          type: "text" as const,
          text: `[FAKE] 笔记检索「${params.query}」结果（硬编码演示数据，非真实笔记）：${JSON.stringify(
            { ...FAKE_VAULT_SEARCH_RESULT, query: params.query },
          )}`,
        },
      ],
      details: { fake: true, tool: "vault_search", query: params.query },
    };
  },
});

export const fakeKnowledgeSearchTool = defineTool({
  name: "knowledge_search",
  label: "知识检索（离线假工具）",
  description:
    "检索工作台知识库文档（只读）。离线假实现：返回硬编码片段，不访问 RAG 索引。",
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

/** Names of every tool the offline SDK-probe harness is allowed to activate. */
export const SMARTCS_TOOL_WHITELIST = ["vault_search", "knowledge_search"] as const;

/** Names that must never be active in the server-side agent. */
export const FORBIDDEN_BUILTIN_TOOLS = ["read", "bash", "edit", "write", "grep", "find", "ls"] as const;

export function createFakeTools() {
  return [fakeVaultSearchTool, fakeKnowledgeSearchTool];
}
