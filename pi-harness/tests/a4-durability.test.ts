/**
 * A4 — When does a file-backed append actually reach disk?
 *
 * Plan v2 §5.2 defines the durability point as "after the corresponding
 * SessionManager append returns" and builds the crash-recovery story (§6.3,
 * F1–F14) on it. This test pins the exact behaviour, including the case where
 * that statement does NOT hold.
 */

import { existsSync, mkdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { SessionManager } from "@earendil-works/pi-coding-agent";
import { makeTmpDir } from "./helpers/harness.js";

function setup(id: string) {
  const root = makeTmpDir("smartcs-a4-");
  const cwd = join(root, "cwd");
  const sessionDir = join(root, "sessions");
  mkdirSync(cwd, { recursive: true });
  mkdirSync(sessionDir, { recursive: true });
  return { root, cwd, sessionDir, sm: SessionManager.create(cwd, sessionDir, { id }) };
}

const readLines = (file: string) =>
  readFileSync(file, "utf-8")
    .split("\n")
    .filter(Boolean)
    .map((l) => JSON.parse(l) as { type: string });

describe("A4 durability point", () => {
  it("persists the very first conversation message before appendMessage returns", () => {
    const { sm } = setup("smartcs-a4-first");
    const file = sm.getSessionFile()!;
    expect(existsSync(file)).toBe(false);

    sm.appendMessage({ role: "user", content: "第一条用户消息", timestamp: Date.now() });

    // Synchronously on disk the moment appendMessage() returned.
    expect(existsSync(file)).toBe(true);
    const lines = readLines(file);
    expect(lines.some((l) => l.type === "message")).toBe(true);
    expect(readFileSync(file, "utf-8")).toContain("第一条用户消息");
  });

  it("appends each later message synchronously — durability point holds per message", () => {
    const { sm } = setup("smartcs-a4-append");
    const file = sm.getSessionFile()!;

    sm.appendMessage({ role: "user", content: "u1", timestamp: Date.now() });
    const afterFirst = statSync(file).size;
    const firstLines = readLines(file).length;

    sm.appendMessage({ role: "assistant", content: "a1", timestamp: Date.now() } as never);
    const afterSecond = statSync(file).size;

    expect(afterSecond).toBeGreaterThan(afterFirst);
    expect(readLines(file).length).toBe(firstLines + 1);
    expect(readFileSync(file, "utf-8")).toContain("a1");
  });

  it("DELTA vs plan §5.2: setup-only entries are NOT durable — no file exists yet", () => {
    const { sm } = setup("smartcs-a4-setup");
    const file = sm.getSessionFile()!;

    sm.appendModelChange("smartcs-openai-compat", "kimi-k2.7-code");
    sm.appendThinkingLevelChange("off");
    sm.appendCustomEntry("smartcs:snapshot", { pendingActionId: "p-1" });

    // append* returned, but nothing is on disk: `_persist` bails until the
    // session contains a user or assistant message (_hasConversation guard).
    expect(existsSync(file)).toBe(false);
  });

  it("an extension-only snapshot written before the first user turn is not durable", () => {
    const { sm } = setup("smartcs-a4-snapshot-then-user");
    const file = sm.getSessionFile()!;

    // This is exactly the Phase 1 shape: extension injects a business snapshot
    // at turn start, before any message exists.
    sm.appendCustomEntry("smartcs:snapshot", { pendingActionId: "p-1", userId: "u-1" });
    expect(existsSync(file)).toBe(false);

    // Once the user message lands, the earlier entries are flushed together
    // with it (the first write replays the whole in-memory entry list).
    sm.appendMessage({ role: "user", content: "hi", timestamp: Date.now() });
    expect(existsSync(file)).toBe(true);
    const raw = readFileSync(file, "utf-8");
    expect(raw).toContain("smartcs:snapshot");
    expect(raw).toContain("pendingActionId");
  });

  it("in-memory sessions never touch disk", () => {
    const { cwd } = setup("smartcs-a4-mem");
    const sm = SessionManager.inMemory(cwd, { id: "smartcs-a4-mem" });
    sm.appendMessage({ role: "user", content: "memory only", timestamp: Date.now() });
    expect(sm.isPersisted()).toBe(false);
    expect(sm.getSessionFile()).toBeUndefined();
  });
});
