import { describe, expect, it } from "vitest";
import { fauxAssistantMessage } from "@earendil-works/pi-ai/providers/faux";
import { createTestAgent } from "./helpers/harness.js";

describe("phase 0 smoke", () => {
  it("runs an agent loop with the faux provider and the fake tools whitelist", async () => {
    const { agent, cleanup } = await createTestAgent([fauxAssistantMessage("你好，我是 SmartCS 客服助手。")]);
    try {
      const events: string[] = [];
      agent.session.subscribe((event) => {
        events.push(event.type);
      });

      await agent.session.prompt("你好");
      expect(agent.session.sessionId).toBeTruthy();
      expect(events).toContain("agent_start");
      expect(events).toContain("agent_settled");
    } finally {
      cleanup();
    }
  }, 30_000);
});
