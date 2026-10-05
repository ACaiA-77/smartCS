/**
 * Turn-snapshot injection (plan v2 §6.7, phase3-design.md §2).
 *
 * The snapshot is fetched by the pipeline BEFORE `prompt()`; this extension only
 * formats an already-in-memory value. That split is deliberate and load-bearing:
 * plan v2 §6.6 forbids awaiting network or database work inside a `pi.on`
 * handler, because handlers are awaited by the dispatcher and would stall the
 * agent loop.
 *
 * Re-injecting every turn is what makes F10 hold: once Pi's compaction drops
 * the history, the authoritative facts are still present, because they were
 * never sourced from the transcript in the first place.
 */

import type { ExtensionAPI, ExtensionFactory } from "@earendil-works/pi-coding-agent";
import type { TurnSnapshot } from "../../business/python-client.js";

export class SnapshotHolder {
  private current: TurnSnapshot | undefined;

  set(snapshot: TurnSnapshot): void {
    this.current = snapshot;
  }

  clear(): void {
    this.current = undefined;
  }

  peek(): TurnSnapshot | undefined {
    return this.current;
  }
}

export const SNAPSHOT_CUSTOM_TYPE = "smartcs.turn_snapshot";

/** Render the blocks into the text the model actually receives. */
export function renderSnapshot(snapshot: TurnSnapshot): string {
  const sections = [...snapshot.blocks]
    .sort((a, b) => a.priority - b.priority)
    .map((block) => `### ${block.title}\n${block.content}`);
  return ["[SmartCS 业务上下文快照 · 权威数据，请以此为准]", ...sections].join("\n\n");
}

export function createContextInjectionExtension(holder: SnapshotHolder): ExtensionFactory {
  return (pi: ExtensionAPI) => {
    pi.on("before_agent_start", () => {
      const snapshot = holder.peek();
      if (!snapshot || snapshot.blocks.length === 0) return undefined;
      // Synchronous: no network, no database, no waiting.
      return {
        message: {
          customType: SNAPSHOT_CUSTOM_TYPE,
          content: renderSnapshot(snapshot),
          // Hidden in the TUI: this is machine context, not conversation.
          display: false,
        },
      };
    });
  };
}

/**
 * Reviewer the compliance extension calls. Only the decision is needed here —
 * `rulesHit` / `llmReviewed` stay on the client's own return type.
 * Returning `undefined` means "no authoritative verdict", and the extension
 * falls back to its local rules.
 */
export type ComplianceReviewer = (
  text: string,
  signal?: AbortSignal,
) => Promise<{ verdict: "pass" | "sanitize" | "fail"; replacement: string | null } | undefined>;
