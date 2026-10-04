/**
 * A8 — Does the tool whitelist actually disable the built-in coding tools in
 * server mode?
 *
 * Plan v2 §8 red line #1: no read/bash/edit/write in production. Verified via
 * (a) the active tool list, (b) the system prompt the model is actually given,
 * (c) the extension-only audit hook, (d) what happens when the model asks for
 * a disabled tool anyway.
 *
 * NOTE on evidence choice: `before_provider_request` cannot be used here. The
 * Faux provider never invokes `onPayload`, so that event is unreachable in an
 * offline test (the real openai-completions provider does invoke it). See
 * tests/a8b-provider-events.test.ts, which pins that limitation explicitly.
 */

import { describe, expect, it } from "vitest";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { fauxAssistantMessage, fauxText, fauxToolCall } from "@earendil-works/pi-ai/providers/faux";
import { createTestAgent } from "./helpers/harness.js";
import { FORBIDDEN_BUILTIN_TOOLS, SMARTCS_TOOL_WHITELIST } from "../src/agent/tools/fake-tools.js";

/** Capture the assembled system prompt handed to the model each turn. */
function systemPromptProbe(seen: string[]) {
  return (pi: ExtensionAPI) => {
    pi.on("before_agent_start", (event) => {
      seen.push(String((event as { systemPrompt?: string }).systemPrompt ?? ""));
      return undefined;
    });
  };
}

describe("A8 built-in tool suppression", () => {
  it("activates only the whitelisted business tools", async () => {
    const { agent, cleanup } = await createTestAgent([fauxAssistantMessage("你好。")]);
    try {
      const active = agent.session.getActiveToolNames();
      expect([...active].sort()).toEqual([...SMARTCS_TOOL_WHITELIST].sort());
      for (const forbidden of FORBIDDEN_BUILTIN_TOOLS) {
        expect(active).not.toContain(forbidden);
      }
      // Stronger than "deactivated": with an explicit allowlist the built-in
      // coding tools are not registered in the tool registry at all — hence
      // the "Tool bash not found" refusal in the last test below.
      const allNames = agent.session.getAllTools().map((t) => t.name);
      for (const forbidden of FORBIDDEN_BUILTIN_TOOLS) {
        expect(allNames).not.toContain(forbidden);
      }
    } finally {
      cleanup();
    }
  });

  it("system prompt the model receives advertises only the whitelisted tools", async () => {
    const seen: string[] = [];
    const { agent, cleanup } = await createTestAgent([fauxAssistantMessage("你好。")], {
      extraExtensions: [systemPromptProbe(seen)],
    });
    try {
      await agent.session.prompt("你好");
      expect(seen).toHaveLength(1);
      const prompt = seen[0]!;

      expect(prompt).toContain("order_query");
      expect(prompt).toContain("knowledge_search");
      for (const forbidden of FORBIDDEN_BUILTIN_TOOLS) {
        // No "Available tools" entry for any coding tool.
        expect(prompt).not.toMatch(new RegExp(`^\\s*[-*]\\s*\`?${forbidden}\`?\\s*[:(-]`, "m"));
      }
    } finally {
      cleanup();
    }
  });

  it("runs a whitelisted tool end-to-end and records the lifecycle in the audit hook", async () => {
    const { agent, cleanup } = await createTestAgent([
      fauxAssistantMessage([fauxText("我帮你查一下。"), fauxToolCall("order_query", { orderId: "1001" })], {
        stopReason: "toolUse",
      }),
      fauxAssistantMessage("你的订单 1001 已发货。"),
    ]);
    try {
      await agent.session.prompt("帮我查订单 1001");

      const context = agent.sessionManager.buildSessionContext();
      const toolResults = context.messages.filter((m) => (m as { role?: string }).role === "toolResult");
      expect(toolResults.length).toBe(1);
      expect(JSON.stringify(toolResults[0])).toContain("[FAKE]");
      expect(JSON.stringify(toolResults[0])).toContain("1001");

      // Extension-only audit events fired (session.subscribe cannot see these).
      expect(agent.audit.records.some((r) => r.kind === "tool_call" && r.toolName === "order_query")).toBe(true);
      expect(agent.audit.records.some((r) => r.kind === "tool_result" && r.toolName === "order_query")).toBe(true);
    } finally {
      cleanup();
    }
  });

  it("refuses a disabled built-in tool the model asks for — nothing is executed", async () => {
    const { agent, cleanup } = await createTestAgent([
      fauxAssistantMessage([fauxToolCall("bash", { command: "rm -rf /" })], { stopReason: "toolUse" }),
      fauxAssistantMessage("抱歉，我无法执行该操作。"),
    ]);
    try {
      await agent.session.prompt("删除所有文件");

      // The audit hook is extension-only and sees every *executed* tool. It
      // never saw bash, so no bash execution happened.
      expect(agent.audit.records.some((r) => r.toolName === "bash")).toBe(false);

      // The SDK answered the call with an error result instead of running it.
      const context = agent.sessionManager.buildSessionContext();
      const toolResults = context.messages.filter(
        (m) => (m as { role?: string }).role === "toolResult",
      ) as Array<{ toolName?: string; isError?: boolean; content?: unknown }>;
      const bashResult = toolResults.find((r) => r.toolName === "bash");
      expect(bashResult).toBeDefined();
      expect(bashResult!.isError).toBe(true);
      expect(JSON.stringify(bashResult!.content)).toContain("not found");

      // The transcript does contain the model's *request* (toolCall arguments)
      // and the refusal, which is correct: the model asked, the runtime said no.
    } finally {
      cleanup();
    }
  });

  it("noTools:'builtin' keeps custom tools while disabling built-ins", async () => {
    const { agent, cleanup } = await createTestAgent([fauxAssistantMessage("你好。")], {
      tools: undefined,
      noTools: "builtin",
    });
    try {
      const active = agent.session.getActiveToolNames();
      for (const forbidden of FORBIDDEN_BUILTIN_TOOLS) {
        expect(active).not.toContain(forbidden);
      }
      expect(active).toContain("order_query");
    } finally {
      cleanup();
    }
  });
});
