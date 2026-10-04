/**
 * A6 — Does `agent_settled` fire exactly once per prompt, including across
 * retries and compaction?
 *
 * Plan v2 §6.1: the completion signal is `agent_settled`, NOT `agent_end`
 * (which fires early / multiple times when a retry happens) and NOT
 * `message_end` (which fires once per LLM call). The receipt flips to
 * `completed` on settle, so an extra or missing settle corrupts the request
 * state machine.
 */

import { describe, expect, it } from "vitest";
import { fauxAssistantMessage, fauxText, fauxToolCall } from "@earendil-works/pi-ai/providers/faux";
import { createTestAgent } from "./helpers/harness.js";

interface Counts {
  agentStart: number;
  agentEnd: number;
  agentSettled: number;
  messageEnd: number;
}

function instrument(seen: string[]): (event: { type: string }) => void {
  return (event) => {
    seen.push(event.type);
  };
}

const tally = (seen: string[]): Counts => ({
  agentStart: seen.filter((t) => t === "agent_start").length,
  agentEnd: seen.filter((t) => t === "agent_end").length,
  agentSettled: seen.filter((t) => t === "agent_settled").length,
  messageEnd: seen.filter((t) => t === "message_end").length,
});

describe("A6 agent_settled frequency", () => {
  it("fires exactly once for a plain prompt", async () => {
    const { agent, cleanup } = await createTestAgent([fauxAssistantMessage("你好，有什么可以帮你？")]);
    const seen: string[] = [];
    try {
      agent.session.subscribe(instrument(seen));
      await agent.session.prompt("你好");

      const counts = tally(seen);
      expect(counts.agentSettled).toBe(1);
      expect(counts.messageEnd).toBeGreaterThanOrEqual(1); // user + assistant
    } finally {
      cleanup();
    }
  });

  it("fires exactly once across a multi-call tool loop (where message_end fires many times)", async () => {
    const { agent, cleanup } = await createTestAgent([
      fauxAssistantMessage([fauxText("我先查一下订单。"), fauxToolCall("order_query", { orderId: "1001" })], {
        stopReason: "toolUse",
      }),
      fauxAssistantMessage("你的订单 1001 已发货。"),
    ]);
    const seen: string[] = [];
    try {
      agent.session.subscribe(instrument(seen));
      await agent.session.prompt("帮我查订单 1001");

      const counts = tally(seen);
      expect(counts.agentSettled).toBe(1);
      // A single prompt produced several message_end events (user, tool-call
      // narration, tool result, final) — proving message_end is not a
      // completion signal.
      expect(counts.messageEnd).toBeGreaterThan(1);
    } finally {
      cleanup();
    }
  });

  it("fires exactly once when the provider error triggers an automatic retry", async () => {
    const { agent, cleanup, faux } = await createTestAgent(
      [
        fauxAssistantMessage("容量不足", {
          stopReason: "error",
          errorMessage: "Selected model is at capacity",
        }),
        fauxAssistantMessage("重试后的答复：订单已发货。"),
      ],
      { settings: { retry: { enabled: true, maxRetries: 3, baseDelayMs: 1 } } },
    );
    const seen: string[] = [];
    try {
      agent.session.subscribe(instrument(seen));
      await agent.session.prompt("帮我查订单");

      const counts = tally(seen);
      // The retry is real: two provider calls happened.
      expect(faux.state.callCount).toBeGreaterThanOrEqual(2);
      expect(counts.agentStart).toBeGreaterThanOrEqual(2);

      // Despite the retry, settle happened exactly once.
      expect(counts.agentSettled).toBe(1);
    } finally {
      cleanup();
    }
  });

  it("fires exactly once when compaction runs mid-prompt", async () => {
    // A tiny context window plus a long first turn forces a real compaction:
    // pi calls the model to summarize, then continues the original prompt.
    const longAnswer = "很长的答复内容。".repeat(400); // ~3200 chars
    const { agent, cleanup, faux } = await createTestAgent(
      [
        fauxAssistantMessage(longAnswer), // turn 1
        fauxAssistantMessage("压缩摘要：用户询问了订单状态。"), // compaction summarizer call
        fauxAssistantMessage("压缩后的答复：订单已发货。"), // turn 2 after compaction
      ],
      {
        fauxModel: { contextWindow: 2000, maxTokens: 500 },
        settings: { compaction: { enabled: true, reserveTokens: 200, keepRecentTokens: 100 } },
      },
    );
    const seen: string[] = [];
    try {
      agent.session.subscribe(instrument(seen));
      await agent.session.prompt("第一轮提问：" + "补充背景。".repeat(200));
      const afterFirst = tally(seen);
      expect(afterFirst.agentSettled).toBe(1);

      const secondSeen: string[] = [];
      const unsub = agent.session.subscribe(instrument(secondSeen));
      await agent.session.prompt("第二轮提问");
      unsub();

      const counts = tally(secondSeen);
      expect(counts.agentSettled).toBe(1);

      // Evidence that compaction actually happened, not just that settle was 1:
      const entryTypes = agent.sessionManager.getEntries().map((e) => e.type);
      expect(entryTypes).toContain("compaction");
      expect(faux.state.callCount).toBeGreaterThanOrEqual(3);
    } finally {
      cleanup();
    }
  });

  it("settles exactly once per prompt across back-to-back prompts", async () => {
    const { agent, cleanup } = await createTestAgent([
      fauxAssistantMessage("第一轮答复。"),
      fauxAssistantMessage("第二轮答复。"),
    ]);
    try {
      await agent.session.prompt("第一轮提问");
      const seen: string[] = [];
      agent.session.subscribe(instrument(seen));
      await agent.session.prompt("第二轮提问");

      const counts = tally(seen);
      expect(counts.agentSettled).toBe(1);
      expect(counts.agentStart).toBe(1);
    } finally {
      cleanup();
    }
  });
});
