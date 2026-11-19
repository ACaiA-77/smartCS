/**
 * Phase 10 §②/§③ acceptance — the model's view of the system.
 *
 * Two invariants that used to be maintained by hand and had drifted apart:
 *
 *   §② the system prompt describes the tool surface that is actually mounted,
 *      and contains no claim that contradicts how the system behaves;
 *   §③ the model-visible tool schemas declare business parameters only —
 *      identity, authorization, idempotency and tracing fields belong to the
 *      runtime and must not appear.
 *
 * These are pure unit cases: no database, no HTTP, no model. They are the
 * regression guard for the drift the closeout review found.
 */

import { describe, expect, it } from "vitest";
import type { ToolDefinition } from "@earendil-works/pi-coding-agent";
import {
  COPILOT_READ_TOOL_NAMES,
  COPILOT_WRITE_TOOL_NAMES,
  createCopilotReadTools,
  createCopilotWriteTools,
} from "../src/agent/tools/copilot-tools.js";
import { copilotPromptFor, copilotSystemPrompt } from "../src/agent/prompt/copilot.js";
import { TurnContext } from "../src/business/turn-context.js";
import type { PythonInternalClient } from "../src/business/python-client.js";

const READ_TOOLS = [...COPILOT_READ_TOOL_NAMES];
const WRITE_TOOLS = [...COPILOT_WRITE_TOOL_NAMES];

/** Fields that belong to the runtime; a model-supplied value is inert. */
const RUNTIME_ONLY_FIELDS = [
  "user_id",
  "business_user_id",
  "account_id",
  "session_id",
  "client_request_id",
  "request_payload_hash",
  "confirmed",
  "approval_id",
  "operation_id",
];

function schemaProperties(tool: ToolDefinition): Record<string, unknown> {
  const parameters = (tool as unknown as { parameters?: { properties?: Record<string, unknown> } }).parameters;
  return parameters?.properties ?? {};
}

function allTools(): ToolDefinition[] {
  const turnContext = new TurnContext();
  // The client is never called: this suite inspects declared schemas only.
  const client = {} as unknown as PythonInternalClient;
  return [
    ...createCopilotReadTools({ client, turnContext }),
    ...createCopilotWriteTools({ mode: "shadow", turnContext }),
  ];
}

describe("Phase 10 §② system prompt", () => {
  it("names every READ and WRITE tool when the write tools are mounted", () => {
    const prompt = copilotSystemPrompt({ writeTools: true });
    for (const name of [...READ_TOOLS, ...WRITE_TOOLS]) {
      expect(prompt, `prompt must name ${name}`).toContain(name);
    }
  });

  it("states the business principle and the two-phase external action flow", () => {
    const prompt = copilotSystemPrompt({ writeTools: true });
    // The load-bearing sentence, in the terms the system actually enforces.
    expect(prompt).toContain("无权自行授权");
    expect(prompt).toContain("是否真正执行由运行时决定");
    // Two-phase external actions: prepare → explicit user confirmation → confirm.
    expect(prompt).toContain("pending_action_id");
    expect(prompt).toContain("copilot_action_prepare");
    expect(prompt).toContain("copilot_action_confirm");
    expect(prompt).toContain("等待用户明确回复确认");
    // Same-turn local write semantics.
    expect(prompt).toContain("vault_append_daily");
  });

  it("does not tell the model it is read-only, or that it cannot report completion", () => {
    const prompt = copilotSystemPrompt({ writeTools: true });
    for (const banned of ["只能做只读", "你只能做只读查询", "你不能声称已经完成", "没有写操作能力"]) {
      expect(prompt, `prompt must not contain "${banned}"`).not.toContain(banned);
    }
  });

  it("never advertises a write tool that is not mounted", () => {
    const prompt = copilotSystemPrompt({ writeTools: false });
    for (const name of WRITE_TOOLS) {
      expect(prompt, `prompt must not advertise ${name} when writes are off`).not.toContain(name);
    }
    expect(prompt).toContain("未启用");
    for (const name of READ_TOOLS) expect(prompt).toContain(name);
  });

  it("renames the knowledge tool for the MCP transport, in every occurrence", () => {
    const bare = copilotSystemPrompt({ writeTools: true });
    const renamed = copilotPromptFor("mcp__knowledge__knowledge_search", { writeTools: true });
    const occurrences = (text: string, needle: string) => text.split(needle).length - 1;

    expect(renamed).toContain("mcp__knowledge__knowledge_search");
    expect(occurrences(renamed, "mcp__knowledge__knowledge_search")).toBe(
      occurrences(bare, "knowledge_search"),
    );
    expect(occurrences(renamed, "mcp__knowledge__knowledge_search")).toBeGreaterThan(0);
    // The default transport is byte-for-byte unchanged by the rename.
    expect(copilotPromptFor("knowledge_search", { writeTools: true })).toBe(bare);
  });
});

describe("Phase 10 §③ model-visible tool schemas", () => {
  it("exposes exactly the 21 Copilot tools, and no others", () => {
    expect(allTools().map((tool) => tool.name).sort()).toEqual([...READ_TOOLS, ...WRITE_TOOLS].sort());
  });

  it("declares business parameters only — no runtime-owned field appears anywhere", () => {
    for (const tool of allTools()) {
      const properties = Object.keys(schemaProperties(tool));
      for (const field of RUNTIME_ONLY_FIELDS) {
        expect(properties, `${tool.name} must not declare ${field}`).not.toContain(field);
      }
    }
  });

  it("pins the exact parameter set for each tool (the adjudicated surface)", () => {
    const expected: Record<string, string[]> = {
      knowledge_search: ["query", "top_k", "domain", "domains"],
      vault_search: ["query", "zone", "limit"],
      vault_read_note: ["path"],
      vault_backlinks: ["target"],
      wiki_lint: [],
      onto_query: ["entity_type", "text", "limit"],
      mail_search: ["account", "query", "folder", "limit"],
      mail_read_thread: ["account", "thread_id"],
      gh_list_notifications: ["all_notifications", "participating", "limit"],
      gh_get_issue_or_pr: ["owner", "repo", "number"],
      gh_search_code: ["query", "repo", "limit"],
      feishu_list_chats: ["page_size"],
      feishu_read_messages: ["container_id", "container_id_type", "page_size"],
      feishu_read_doc: ["document_id"],
      vault_append_daily: ["entry", "date_str"],
      vault_propose_edit: ["target_rel_path", "rationale", "proposed_content"],
      wiki_compile_doc: ["source_uri", "title", "raw_content", "summary_markdown", "slug", "related_entities"],
      onto_upsert: ["entity_type", "name", "canonical_key", "attrs"],
      mail_save_draft: ["account", "to", "subject", "body", "cc", "reply_to_message_id"],
      copilot_action_prepare: ["tool", "arguments"],
      copilot_action_confirm: ["pending_action_id"],
    };
    for (const tool of allTools()) {
      expect(Object.keys(schemaProperties(tool)).sort(), tool.name).toEqual(expected[tool.name]!.sort());
    }
  });

  it("still refuses unknown fields (P2-3 must keep holding)", () => {
    for (const tool of allTools()) {
      const parameters = (tool as unknown as { parameters: { additionalProperties?: boolean } }).parameters;
      expect(parameters.additionalProperties, `${tool.name} must stay closed`).toBe(false);
    }
  });
});
