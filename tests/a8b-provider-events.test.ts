/**
 * Provider-level extension events under the Faux provider.
 *
 * Plan v2 §6.9 wants TS spans for model calls, and §7 wants request-level
 * control. Pi exposes `before_provider_request` / `after_provider_response` as
 * extension events, wired through the provider's `onPayload` / `onResponse`
 * callbacks. The Faux provider implements only `onResponse`, so half of that
 * surface is UNREACHABLE in an offline test.
 *
 * This is pinned here so Phase 6 does not plan tracing around an event it can
 * only exercise against a live provider.
 */

import { describe, expect, it } from "vitest";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { fauxAssistantMessage } from "@earendil-works/pi-ai/providers/faux";
import { createTestAgent } from "./helpers/harness.js";

function providerEventProbe(seen: { before: number; after: number }) {
  return (pi: ExtensionAPI) => {
    pi.on("before_provider_request", () => {
      seen.before += 1;
      return undefined;
    });
    pi.on("after_provider_response", () => {
      seen.after += 1;
      return undefined;
    });
  };
}

describe("provider events under Faux", () => {
  it("after_provider_response fires, before_provider_request does NOT", async () => {
    const seen = { before: 0, after: 0 };
    const { agent, cleanup } = await createTestAgent([fauxAssistantMessage("答复。")], {
      extraExtensions: [providerEventProbe(seen)],
    });
    try {
      await agent.session.prompt("提问");

      // Providers must call `onResponse`; faux does.
      expect(seen.after).toBeGreaterThan(0);

      // Faux never calls `onPayload`, so the payload-rewrite event cannot be
      // exercised offline. The real openai-completions provider does call it
      // (dist/api/openai-completions.js invokes options.onPayload).
      expect(seen.before).toBe(0);
    } finally {
      cleanup();
    }
  });
});
