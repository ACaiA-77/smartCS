/**
 * A7 — Does reopening a session restore the ACTIVE BRANCH?
 *
 * Plan v2 §6.1/§6.3: after a crash or idle dispose, the harness reopens the
 * session by id and continues from the active branch. Plan §6.8 further needs
 * the branch structure to project user-visible history.
 *
 * Every assertion here observes a *real* reopen through a fresh runtime, not a
 * re-read of the same in-memory manager.
 */

import { describe, expect, it } from "vitest";
import { SessionManager, type SessionMessageEntry } from "@earendil-works/pi-coding-agent";
import { fauxAssistantMessage } from "@earendil-works/pi-ai/providers/faux";
import { createTestAgent } from "./helpers/harness.js";
import { extractMessageText } from "../src/streaming/status.js";

const textOf = (m: unknown) => extractMessageText(m);
const roles = (messages: unknown[]) => messages.map((m) => (m as { role?: string }).role);

describe("A7 reopen restores active branch", () => {
  it("reopens by file path with the same id, leaf and resolved context", async () => {
    const first = await createTestAgent(
      [fauxAssistantMessage("第一轮答复。"), fauxAssistantMessage("第二轮答复。")],
      { sessionId: "smartcs-a7-reopen" },
    );

    let file: string;
    let leafId: string | null;
    let entryCount: number;
    let contextTexts: string[];

    try {
      await first.agent.session.prompt("第一轮提问");
      await first.agent.session.prompt("第二轮提问");

      const sm = first.agent.sessionManager;
      expect(sm.getSessionId()).toBe("smartcs-a7-reopen");
      file = sm.getSessionFile()!;
      leafId = sm.getLeafId();
      entryCount = sm.getEntries().length;
      contextTexts = sm.buildSessionContext().messages.map(textOf);
    } finally {
      // Release the runtime, KEEP the files for the reopen below.
      first.disposeSession();
    }

    const reopened = await createTestAgent([], { sessionManager: SessionManager.open(file!) });
    try {
      expect(reopened.agent.sessionManager.getSessionId()).toBe("smartcs-a7-reopen");
      expect(reopened.agent.sessionManager.getLeafId()).toBe(leafId!);
      expect(reopened.agent.sessionManager.getEntries().length).toBe(entryCount!);
      expect(reopened.agent.session.getActiveToolNames().sort()).toEqual(["knowledge_search", "order_query"]);

      const reopenedTexts = reopened.agent.sessionManager.buildSessionContext().messages.map(textOf);
      expect(reopenedTexts).toEqual(contextTexts!);
      expect(reopenedTexts).toContain("第一轮答复。");
      expect(reopenedTexts).toContain("第二轮答复。");
    } finally {
      reopened.cleanup();
      first.cleanup();
    }
  });

  it("restores the post-branch leaf, not the abandoned path", async () => {
    const first = await createTestAgent(
      [fauxAssistantMessage("第一轮答复。"), fauxAssistantMessage("被放弃的分支答复。")],
      { sessionId: "smartcs-a7-branch" },
    );

    let file: string;
    let cwd: string;
    let sessionDir: string;
    let branchLeaf: string | null;

    try {
      await first.agent.session.prompt("第一轮提问");
      await first.agent.session.prompt("第二轮提问");

      const sm = first.agent.sessionManager;
      file = sm.getSessionFile()!;
      cwd = first.agent.paths.runtimeCwd;
      sessionDir = first.agent.paths.sessionDir;

      // Move the leaf back to the first assistant message and append a new
      // user message: creates a second branch, abandons turn 2.
      const entries = sm.getEntries() as SessionMessageEntry[];
      const firstAssistant = entries.find((e) => e.type === "message" && e.message.role === "assistant");
      expect(firstAssistant).toBeDefined();

      sm.branch(firstAssistant!.id);
      sm.appendMessage({ role: "user", content: "分支上的新提问", timestamp: Date.now() });
      branchLeaf = sm.getLeafId();

      // The abandoned path is still on disk (append-only tree), so a naive
      // "last entry wins" reopen would pick the wrong branch.
      expect(branchLeaf).not.toBe(entries.at(-1)!.id);
    } finally {
      first.disposeSession();
    }

    // 1) Reopen by id lookup — the path is recovered, not remembered.
    const byId = SessionManager.findById(cwd!, "smartcs-a7-branch", sessionDir!);
    expect(byId).toBe(file!);

    const reopenedSm = SessionManager.open(byId!);
    expect(reopenedSm.getSessionId()).toBe("smartcs-a7-branch");
    expect(reopenedSm.getLeafId()).toBe(branchLeaf!);

    // 2) A full runtime reopen sees the branch-resolved context.
    const reopened = await createTestAgent([], { sessionManager: SessionManager.open(file!) });
    try {
      expect(reopened.agent.sessionManager.getLeafId()).toBe(branchLeaf!);
      const texts = reopened.agent.sessionManager.buildSessionContext().messages.map(textOf);
      expect(texts).toContain("第一轮答复。");
      expect(texts).toContain("分支上的新提问");
      // The abandoned turn-2 assistant message must not be in the model context.
      expect(texts).not.toContain("被放弃的分支答复。");
    } finally {
      reopened.cleanup();
      first.cleanup();
    }
  });

  it("HAZARD: opening a missing path silently creates a NEW empty session", async () => {
    const { agent, disposeSession, cleanup } = await createTestAgent([fauxAssistantMessage("答复。")], {
      sessionId: "smartcs-a7-missing",
    });
    let missingPath: string;
    try {
      await agent.session.prompt("提问");
      missingPath = agent.sessionManager.getSessionFile()!;
      expect(agent.sessionManager.getSessionId()).toBe("smartcs-a7-missing");
    } finally {
      disposeSession();
    }

    // Windows path spelled with a directory that does not exist.
    const gone = missingPath!.replace(/[^\\/]+\.jsonl$/, "does-not-exist.jsonl");

    // No throw, no diagnostic: a brand-new session with a DIFFERENT id is
    // created at the requested path. A Phase 1 recovery path that opens a
    // session file must therefore verify existence / session id itself.
    const sm = SessionManager.open(gone);
    expect(sm.getSessionId()).not.toBe("smartcs-a7-missing");
    expect(sm.getEntries()).toHaveLength(0);

    cleanup();
  });
});
