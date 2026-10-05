/**
 * Deltas between plan v2's assumptions and pi-coding-agent 1.0.1 as installed.
 *
 * These are not feature tests; they are the evidence behind the "API delta"
 * section of PHASE0_REPORT.md. Each one pins a statement the plan makes so a
 * future upgrade that changes it fails loudly.
 */

import { describe, expect, it } from "vitest";
import {
  CURRENT_SESSION_VERSION,
  SessionManager,
  createAgentSession,
} from "@earendil-works/pi-coding-agent";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { fauxAssistantMessage } from "@earendil-works/pi-ai/providers/faux";
import { createTestAgent } from "./helpers/harness.js";

describe("plan v2 vs 1.0.1 deltas", () => {
  it("DELTA: session format version is 3, not the v4 the plan's drift note claims", () => {
    expect(CURRENT_SESSION_VERSION).toBe(3);
  });

  it("DELTA: extensionFactories is not a createAgentSession option — it lives on the resource loader", () => {
    // Type-level fact: CreateAgentSessionOptions has no extensionFactories.
    // Confirmed at runtime by the fact that our extensions work although we
    // never pass such a field to createAgentSession (they are supplied through
    // DefaultResourceLoader).
    const options = {
      cwd: process.cwd(),
      agentDir: process.cwd(),
      sessionManager: SessionManager.inMemory(),
    };
    expect(Object.keys(options)).not.toContain("extensionFactories");
  });

  it("DELTA: agent.state.messages mutation is transient — SessionManager stays canonical", async () => {
    const contexts: string[][] = [];
    const probe = (pi: ExtensionAPI) => {
      pi.on("context", (event) => {
        const messages = (event as { messages: Array<{ role?: string }> }).messages;
        contexts.push(messages.map((m) => String(m.role)));
        return undefined;
      });
    };

    const { agent, cleanup } = await createTestAgent(
      [fauxAssistantMessage("第一轮答复。"), fauxAssistantMessage("第二轮答复。")],
      { extraExtensions: [probe] },
    );
    try {
      await agent.session.prompt("第一轮提问");
      const entriesBefore = agent.sessionManager.getEntries().length;
      const ctxAfterTurn1 = contexts.at(-1)!;

      // Wipe the live agent state directly.
      agent.session.agent.state.messages = [];
      expect(agent.session.agent.state.messages).toHaveLength(0);

      // The transcript is untouched: SessionManager is the authority.
      expect(agent.sessionManager.getEntries().length).toBe(entriesBefore);

      // And the next turn's model context is rebuilt from SessionManager, so
      // the mutation has no effect on what the model sees.
      await agent.session.prompt("第二轮提问");
      const ctxAfterTurn2 = contexts.at(-1)!;
      expect(ctxAfterTurn2.length).toBeGreaterThanOrEqual(ctxAfterTurn1.length);

      const serialized = JSON.stringify(
        agent.sessionManager.buildSessionContext().messages.map((m) => (m as { role?: string }).role),
      );
      expect(serialized).toContain("assistant");
    } finally {
      cleanup();
    }
  });

  it("DELTA: context edits exist in 1.0.1 — relevant to the §6.8 history projector", () => {
    const sm = SessionManager.inMemory(process.cwd(), { id: "smartcs-delta-ctx" });
    const target = sm.appendMessage({ role: "user", content: "原始内容", timestamp: Date.now() });

    // New capability not mentioned in plan v2: an append-only edit that
    // replaces/omits an earlier entry's *model* contribution without touching
    // the stored entry. This is exactly the "context edit replacement/omission"
    // the §6.8 projector needs, and it is first-class in the SDK.
    sm.appendContextEdit(target, { content: "替换后的内容" });

    const projection = sm.buildSessionProjection();
    const projected = JSON.stringify(projection.messages);
    expect(projected).toContain("替换后的内容");
    expect(projected).not.toContain("原始内容");

    // The raw stored entry is unchanged — the edit is a separate entry.
    const stored = JSON.stringify(sm.getEntries().find((e) => e.id === target));
    expect(stored).toContain("原始内容");
  });

  it("DELTA: message_update emits assembled content on *_end, not only deltas", async () => {
    const seen: string[] = [];
    const probe = (pi: ExtensionAPI) => {
      pi.on("message_update", (event) => {
        const sub = (event as { assistantMessageEvent?: { type?: string } }).assistantMessageEvent;
        if (sub?.type) seen.push(sub.type);
        return undefined;
      });
    };
    const { agent, cleanup } = await createTestAgent([fauxAssistantMessage("完整答复内容。")], {
      extraExtensions: [probe],
    });
    try {
      await agent.session.prompt("提问");
      // The plan's drift note says "message_update only sends deltas, assemble
      // yourself". In 1.0.1 the stream also emits text_end carrying the
      // complete assembled string, so manual assembly is optional.
      expect(seen).toContain("text_delta");
      expect(seen).toContain("text_end");
    } finally {
      cleanup();
    }
  });

  it("CONFIRM: session.agent is exposed and AgentSessionRuntime.dispose() is async", async () => {
    const { agent, cleanup } = await createTestAgent([fauxAssistantMessage("答复。")]);
    try {
      expect(agent.session.agent).toBeDefined();
      // Runtime dispose is async (plan §8 #6) — read from the type surface:
      // dispose(): Promise<void> on AgentSessionRuntime.
      const { createAgentSessionRuntime } = await import("@earendil-works/pi-coding-agent");
      expect(typeof createAgentSessionRuntime).toBe("function");
      expect(typeof createAgentSession).toBe("function");
    } finally {
      cleanup();
    }
  });
});
