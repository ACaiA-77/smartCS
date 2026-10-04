/**
 * Phase 0 acceptance checklist (plan v2 §10 Phase 0 "验收").
 *
 *   agent loop 可跑 · session 可外部恢复 · tool_call hook 正常
 *   无 bash/read/write/edit · SSE 生命周期正常
 *
 * Plus the server-side de-coding-assistant requirements from §8: the system
 * prompt must replace pi's coding persona, and the six extension-only events
 * must be unreachable from `session.subscribe` (which fails silently).
 */

import { describe, expect, it } from "vitest";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { fauxAssistantMessage, fauxText, fauxToolCall } from "@earendil-works/pi-ai/providers/faux";
import { createTestAgent } from "./helpers/harness.js";
import { ChatStream } from "../src/streaming/chat-stream.js";
import { FORBIDDEN_BUILTIN_TOOLS } from "../src/agent/tools/fake-tools.js";

describe("Phase 0 acceptance", () => {
  it("overrides pi's default coding persona in the assembled system prompt", async () => {
    const prompts: string[] = [];
    const probe = (pi: ExtensionAPI) => {
      pi.on("before_agent_start", (event) => {
        prompts.push(String((event as { systemPrompt?: string }).systemPrompt ?? ""));
        return undefined;
      });
    };
    const { agent, cleanup } = await createTestAgent([fauxAssistantMessage("你好。")], {
      extraExtensions: [probe],
    });
    try {
      await agent.session.prompt("你是谁？");
      const prompt = prompts[0]!;

      // Our identity is present...
      expect(prompt).toContain("SmartCS 智能客服助手");
      // ...and pi's coding-assistant framing is gone.
      expect(prompt).not.toMatch(/expert coding assistant/i);
      expect(prompt).not.toMatch(/operating inside pi/i);
      expect(prompt).not.toMatch(/coding agent/i);
    } finally {
      cleanup();
    }
  });

  it("runs the agent loop and produces a JSON final", async () => {
    const { agent, cleanup } = await createTestAgent([fauxAssistantMessage("你的订单已发货。")]);
    try {
      const stream = new ChatStream(agent.session);
      const result = await stream.run("订单状态？");

      expect(result.settledCount).toBe(1);
      expect(result.finalText).toBe("你的订单已发货。");
      expect(result.frames.map((f) => f.type)).toEqual(["final", "done"]);
    } finally {
      cleanup();
    }
  });

  it("emits deterministic status frames before the final when a tool runs", async () => {
    const { agent, cleanup } = await createTestAgent([
      fauxAssistantMessage([fauxText("我查一下。"), fauxToolCall("order_query", { orderId: "1001" })], {
        stopReason: "toolUse",
      }),
      fauxAssistantMessage("订单 1001 已发货。"),
    ]);
    try {
      const stream = new ChatStream(agent.session);
      const result = await stream.run("查订单 1001");

      expect(result.frames.map((f) => f.type)).toEqual(["status", "final", "done"]);
      const status = result.frames[0] as { type: "status"; text: string };
      expect(status.text).toBe("正在查询订单");
      expect(result.finalText).toBe("订单 1001 已发货。");
    } finally {
      cleanup();
    }
  });

  it("sends the compliance-approved message as final, never the raw one", async () => {
    const { agent, cleanup } = await createTestAgent([fauxAssistantMessage("泄漏内容 sk-abcdefgh12345")], {
      compliance: { bannedPatterns: [/sk-[A-Za-z0-9]{8,}/], sanitizedText: "已拦截。" },
    });
    try {
      const stream = new ChatStream(agent.session);
      const result = await stream.run("测试");
      expect(result.finalText).toBe("已拦截。");
    } finally {
      cleanup();
    }
  });

  it("falls back to a deterministic safe final when no eligible candidate exists", async () => {
    // The only assistant message is a tool-call narration -> not eligible.
    const { agent, cleanup } = await createTestAgent([
      fauxAssistantMessage([fauxToolCall("order_query", { orderId: "1001" })], { stopReason: "toolUse" }),
      fauxAssistantMessage([], { stopReason: "length" }), // abnormal stop, empty content
    ]);
    try {
      const stream = new ChatStream(agent.session);
      const result = await stream.run("查订单");
      expect(result.finalText).toBe("抱歉，我这边暂时无法给出答复，请稍后再试或转人工客服。");
    } finally {
      cleanup();
    }
  });

  it("tool_call / tool_result are unreachable from session.subscribe (extension-only)", async () => {
    const { agent, cleanup } = await createTestAgent([
      fauxAssistantMessage([fauxToolCall("order_query", { orderId: "1001" })], { stopReason: "toolUse" }),
      fauxAssistantMessage("好了。"),
    ]);
    try {
      const subscribeTypes = new Set<string>();
      agent.session.subscribe((event) => {
        subscribeTypes.add(event.type);
      });
      await agent.session.prompt("查订单 1001");

      // Plan v2 §8 #7: these never arrive on the public listener, and the
      // failure is silent — no error, the branch simply never matches.
      expect(subscribeTypes.has("tool_call")).toBe(false);
      expect(subscribeTypes.has("tool_result")).toBe(false);
      // The tool DID execute, as shown by the extension-only audit hook.
      expect(agent.audit.records.some((r) => r.kind === "tool_call")).toBe(true);
    } finally {
      cleanup();
    }
  });

  it("abort stops the run, still settles exactly once, and reports the aborted stopReason", async () => {
    const { agent, cleanup } = await createTestAgent([
      async (_ctx: unknown, options: { signal?: AbortSignal } | undefined) => {
        await new Promise((resolve) => setTimeout(resolve, 500));
        if (options?.signal?.aborted) {
          return fauxAssistantMessage("已中止", { stopReason: "aborted", errorMessage: "Request was aborted" });
        }
        return fauxAssistantMessage("正常完成");
      },
    ]);
    try {
      const stream = new ChatStream(agent.session);
      const runPromise = stream.run("一个很慢的问题");
      setTimeout(() => {
        void stream.abort();
      }, 50);
      const result = await runPromise;

      expect(result.aborted).toBe(true);
      expect(result.settledCount).toBe(1);
      expect(result.frames.map((f) => f.type)).toContain("done");
      const done = result.frames.at(-1) as { type: "done"; reason: string };
      expect(done.reason).toBe("aborted");
    } finally {
      cleanup();
    }
  });

  it("keeps every forbidden built-in tool out of the active set", async () => {
    const { agent, cleanup } = await createTestAgent([fauxAssistantMessage("你好。")]);
    try {
      const active = agent.session.getActiveToolNames();
      for (const forbidden of FORBIDDEN_BUILTIN_TOOLS) {
        expect(active).not.toContain(forbidden);
      }
      expect(agent.session.getActiveToolNames()).toHaveLength(2);
    } finally {
      cleanup();
    }
  });
});
