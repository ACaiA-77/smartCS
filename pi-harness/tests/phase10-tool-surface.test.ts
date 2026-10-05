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
import { createBusinessReadTools } from "../src/agent/tools/business-tools.js";
import { createShadowWriteTools } from "../src/agent/tools/shadow-write-tools.js";
import { smartCsSystemPrompt, systemPromptFor } from "../src/agent/prompt/customer-service.js";
import { TurnContext } from "../src/business/turn-context.js";
import type { PythonInternalClient } from "../src/business/python-client.js";

const READ_TOOLS = ["order_query", "knowledge_search", "ticket_query", "refund_evaluate", "risk_check"];
const WRITE_TOOLS = ["refund_confirm", "ticket_create"];

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
    ...createBusinessReadTools({ client, turnContext }),
    ...createShadowWriteTools({ mode: "shadow", turnContext }),
  ];
}

describe("Phase 10 §② system prompt", () => {
  it("names every READ and WRITE tool when the write tools are mounted", () => {
    const prompt = smartCsSystemPrompt({ writeTools: true });
    for (const name of [...READ_TOOLS, ...WRITE_TOOLS]) {
      expect(prompt, `prompt must name ${name}`).toContain(name);
    }
  });

  it("states the business principle and the two-phase refund flow", () => {
    const prompt = smartCsSystemPrompt({ writeTools: true });
    // The load-bearing sentence, in the terms the system actually enforces.
    expect(prompt).toContain("无权自行授权");
    expect(prompt).toContain("写操作是否真正执行");
    // Two-phase refund: evaluate → explicit user confirmation → confirm.
    expect(prompt).toContain("pending_action_id");
    expect(prompt).toContain("refund_evaluate");
    expect(prompt).toContain("refund_confirm");
    expect(prompt).toContain("等待用户明确回复确认");
    // Same-turn ticket semantics.
    expect(prompt).toContain("ticket_create");
  });

  it("does not tell the model it is read-only, or that it cannot report completion", () => {
    // These two phrasings were the defect: with write tools mounted and
    // SMARTCS_WRITE_MODE=live they instructed the model to deny, or refuse,
    // capabilities it actually had.
    const prompt = smartCsSystemPrompt({ writeTools: true });
    for (const banned of ["只能做只读", "你只能做只读查询", "你不能声称已经完成", "没有写操作能力"]) {
      expect(prompt, `prompt must not contain "${banned}"`).not.toContain(banned);
    }
  });

  it("never advertises a write tool that is not mounted", () => {
    const prompt = smartCsSystemPrompt({ writeTools: false });
    for (const name of WRITE_TOOLS) {
      expect(prompt, `prompt must not advertise ${name} when writes are off`).not.toContain(name);
    }
    // …and says so plainly rather than staying silent about refunds/tickets.
    expect(prompt).toContain("未启用");
    for (const name of READ_TOOLS) expect(prompt).toContain(name);
  });

  it("renames the knowledge tool for the MCP transport, in every occurrence", () => {
    const bare = smartCsSystemPrompt({ writeTools: true });
    const renamed = systemPromptFor("mcp__knowledge__knowledge_search", { writeTools: true });
    const occurrences = (text: string, needle: string) => text.split(needle).length - 1;

    expect(renamed).toContain("mcp__knowledge__knowledge_search");
    // `replaceAll`, not `replace`: the renamed name must appear exactly as
    // often as the bare name did. (A substring check is impossible here —
    // "mcp__knowledge__knowledge_search" contains "knowledge_search" — so the
    // count is the honest assertion.)
    expect(occurrences(renamed, "mcp__knowledge__knowledge_search")).toBe(
      occurrences(bare, "knowledge_search"),
    );
    expect(occurrences(renamed, "mcp__knowledge__knowledge_search")).toBeGreaterThan(0);
    // The default transport is byte-for-byte unchanged by the rename.
    expect(systemPromptFor("knowledge_search", { writeTools: true })).toBe(bare);
  });
});

describe("Phase 10 §③ model-visible tool schemas", () => {
  it("exposes exactly the seven tools, and no others", () => {
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

  it("pins the exact parameter set for each tool (the review's adjudicated surface)", () => {
    const expected: Record<string, string[]> = {
      order_query: ["order_id"],
      refund_evaluate: ["order_id"],
      knowledge_search: ["query", "top_k", "domain", "domains"],
      ticket_query: ["ticket_id"],
      risk_check: ["action", "amount"],
      refund_confirm: ["pending_action_id"],
      ticket_create: ["title", "description", "priority", "category"],
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
