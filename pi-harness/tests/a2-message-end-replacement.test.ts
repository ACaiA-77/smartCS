/**
 * A2 — Can a `message_end` extension return a REPLACEMENT message?
 *
 * Plan v2 §6.5 (Compliance-First Streaming) is load-bearing on this: the
 * compliance review sanitizes the final assistant message inside `message_end`
 * and the SDK is expected to persist the sanitized version, so that
 * "what the user sees == what the transcript stores".
 */

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { fauxAssistantMessage } from "@earendil-works/pi-ai/providers/faux";
import { createTestAgent } from "./helpers/harness.js";
import { extractMessageText } from "../src/streaming/status.js";

const LEAKY_ANSWER = "好的，你的密钥 sk-abcdefgh12345 已确认，订单会尽快处理。";
const SANITIZED = "（该内容已被安全策略拦截，请重新描述你的问题。）";

describe("A2 message_end replacement", () => {
  it("returns a replacement and the replacement — not the original — is persisted", async () => {
    const { agent, cleanup } = await createTestAgent([fauxAssistantMessage(LEAKY_ANSWER)], {
      compliance: { bannedPatterns: [/sk-[A-Za-z0-9]{8,}/], sanitizedText: SANITIZED },
    });
    try {
      await agent.session.prompt("我的密钥是 sk-abcdefgh12345，帮我查订单");

      // 1) the hook fired and produced a replacement
      expect(agent.compliance.replacementCount).toBe(1);
      const record = agent.compliance.records.find((r) => r.action === "sanitize");
      expect(record?.originalText).toContain("sk-abcdefgh12345");
      expect(record?.finalText).toBe(SANITIZED);

      // 2) the in-memory session state holds the replacement
      const assistantEntry = agent.sessionManager
        .getEntries()
        .filter((e) => e.type === "message" && (e as { message: { role: string } }).message.role === "assistant");
      const lastAssistant = assistantEntry.at(-1) as { message: unknown };
      expect(extractMessageText(lastAssistant.message)).toBe(SANITIZED);

      // 3) the model context built for the next turn also holds the replacement
      const context = agent.sessionManager.buildSessionContext();
      const contextAssistant = context.messages.filter((m) => (m as { role?: string }).role === "assistant");
      expect(extractMessageText(contextAssistant.at(-1))).toBe(SANITIZED);

      // 4) the JSONL transcript on disk holds the replacement, not the leak.
      //    Checked per-entry: compliance rewrites ASSISTANT output only. The
      //    raw USER message stays verbatim on purpose (plan v2 §6.6 keeps raw
      //    user provenance), so a whole-file substring check would be wrong.
      const file = agent.sessionManager.getSessionFile()!;
      const parsed = readFileSync(file, "utf-8")
        .split("\n")
        .filter(Boolean)
        .map((line) => JSON.parse(line) as { type: string; message?: { role?: string; content?: unknown } });
      const assistantLines = parsed.filter((e) => e.type === "message" && e.message?.role === "assistant");
      const userLines = parsed.filter((e) => e.type === "message" && e.message?.role === "user");

      expect(assistantLines).toHaveLength(1);
      const assistantRaw = JSON.stringify(assistantLines[0]);
      expect(assistantRaw).toContain(SANITIZED);
      expect(assistantRaw).not.toContain("sk-abcdefgh12345");

      // Documented behaviour, asserted so a future change is noticed:
      // the user's own raw message is still stored verbatim.
      expect(userLines).toHaveLength(1);
      expect(JSON.stringify(userLines[0])).toContain("sk-abcdefgh12345");
    } finally {
      cleanup();
    }
  });

  it("leaves a clean message untouched (no spurious replacement)", async () => {
    const { agent, cleanup } = await createTestAgent([fauxAssistantMessage("你的订单已发货，预计明天送达。")], {
      compliance: { bannedPatterns: [/sk-[A-Za-z0-9]{8,}/], sanitizedText: SANITIZED },
    });
    try {
      await agent.session.prompt("我的订单到哪了？");
      expect(agent.compliance.replacementCount).toBe(0);
      expect(agent.compliance.records.every((r) => r.action === "pass")).toBe(true);

      const file = agent.sessionManager.getSessionFile()!;
      expect(readFileSync(file, "utf-8")).toContain("你的订单已发货");
    } finally {
      cleanup();
    }
  });
});
