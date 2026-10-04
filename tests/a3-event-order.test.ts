/**
 * A3 — Event order for a message: extension `message_end` → public listeners →
 * `SessionManager.appendMessage`.
 *
 * Plan v2 §6.5/§6.6 depend on the ordering: compliance rewrites inside the
 * extension and the rewrite must be what gets persisted; the public listener
 * must observe the post-compliance message.
 */

import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { SessionManager, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { fauxAssistantMessage } from "@earendil-works/pi-ai/providers/faux";
import { makeTmpDir, createTestAgent } from "./helpers/harness.js";
import { extractMessageText } from "../src/streaming/status.js";

interface Observation {
  who: "extension" | "listener";
  text: string;
  persistedMessagesAtThisPoint: number;
}

describe("A3 message_end ordering", () => {
  it("runs extension hook, then public listener, then persists — in that order", async () => {
    const root = makeTmpDir("smartcs-a3-");
    const cwd = join(root, "cwd");
    const sessionDir = join(root, "sessions");
    mkdirSync(cwd, { recursive: true });
    mkdirSync(sessionDir, { recursive: true });

    const sessionManager = SessionManager.create(cwd, sessionDir, { id: "smartcs-a3-order" });
    const observations: Observation[] = [];
    const persistedAt = () =>
      sessionManager.getEntries().filter((e) => e.type === "message").length;

    // Extension layer (pi.on) — extension-only visibility, runs first.
    const probeExtension = (pi: ExtensionAPI) => {
      pi.on("message_end", (event) => {
        const message = (event as { message: unknown }).message;
        if ((message as { role?: string }).role !== "assistant") return undefined;
        observations.push({
          who: "extension",
          text: extractMessageText(message),
          persistedMessagesAtThisPoint: persistedAt(),
        });
        return undefined;
      });
    };

    const { agent, cleanup } = await createTestAgent([fauxAssistantMessage("最终答复：订单已发货。")], {
      sessionManager,
      extraExtensions: [probeExtension],
    });

    try {
      // Public listener layer (session.subscribe).
      agent.session.subscribe((event) => {
        if (event.type !== "message_end") return;
        const message = (event as { message: unknown }).message;
        if ((message as { role?: string }).role !== "assistant") return;
        observations.push({
          who: "listener",
          text: extractMessageText(message),
          persistedMessagesAtThisPoint: persistedAt(),
        });
      });

      await agent.session.prompt("订单状态？");

      const ext = observations.find((o) => o.who === "extension");
      const listener = observations.find((o) => o.who === "listener");
      expect(ext).toBeDefined();
      expect(listener).toBeDefined();

      // 1) extension handler ran before the public listener
      expect(observations.indexOf(ext!)).toBeLessThan(observations.indexOf(listener!));

      // 2) at BOTH observation points the assistant message was not yet appended
      expect(ext!.persistedMessagesAtThisPoint).toBe(listener!.persistedMessagesAtThisPoint);
      const persistedAfterPrompt = persistedAt();
      expect(persistedAfterPrompt).toBe(ext!.persistedMessagesAtThisPoint + 1);

      // 3) both layers observed the same (possibly rewritten) text
      expect(ext!.text).toBe(listener!.text);
      expect(ext!.text).toContain("订单已发货");
    } finally {
      cleanup();
    }
  });
});
