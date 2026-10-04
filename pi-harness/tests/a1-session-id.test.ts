/**
 * A1 — `SessionManager.create(cwd, dir, { id })` accepts a caller-supplied id,
 * and whether the session id → file path mapping is derivable.
 *
 * Plan v2 §5.2 is load-bearing on this: SmartCS session_id is used directly as
 * the Pi session id with NO mapping table. If the path were not recoverable,
 * Phase 1 would need the `pi_session_registry` fallback table.
 */

import { existsSync, mkdirSync } from "node:fs";
import { basename, join } from "node:path";
import { describe, expect, it } from "vitest";
import { SessionManager } from "@earendil-works/pi-coding-agent";
import { makeTmpDir } from "./helpers/harness.js";

function setup() {
  const root = makeTmpDir("smartcs-a1-");
  const cwd = join(root, "cwd");
  const sessionDir = join(root, "sessions");
  mkdirSync(cwd, { recursive: true });
  mkdirSync(sessionDir, { recursive: true });
  return { root, cwd, sessionDir };
}

const SESSION_ID = "smartcs-conv-0f8a1c";

describe("A1 caller-supplied session id", () => {
  it("uses the caller id verbatim as the Pi session id", () => {
    const { cwd, sessionDir } = setup();
    const sm = SessionManager.create(cwd, sessionDir, { id: SESSION_ID });
    expect(sm.getSessionId()).toBe(SESSION_ID);
    expect(sm.getHeader()?.id).toBe(SESSION_ID);
  });

  it("names the file <timestamp>_<id>.jsonl inside the explicit session dir", () => {
    const { cwd, sessionDir } = setup();
    const sm = SessionManager.create(cwd, sessionDir, { id: SESSION_ID });
    const file = sm.getSessionFile();
    expect(file).toBeTruthy();
    expect(file!.startsWith(sessionDir)).toBe(true);
    expect(basename(file!)).toMatch(/^\d{4}-\d{2}-\d{2}T[\d-]+Z_smartcs-conv-0f8a1c\.jsonl$/);
  });

  it("does NOT write the file until a user/assistant message is appended", () => {
    const { cwd, sessionDir } = setup();
    const sm = SessionManager.create(cwd, sessionDir, { id: SESSION_ID });
    const file = sm.getSessionFile()!;
    expect(existsSync(file)).toBe(false);

    // A setup-only entry is not enough to create the file.
    sm.appendModelChange("smartcs-openai-compat", "kimi-k2.7-code");
    expect(existsSync(file)).toBe(false);

    sm.appendMessage({ role: "user", content: "你好", timestamp: Date.now() });
    expect(existsSync(file)).toBe(true);
  });

  it("recovers the file path by id via SessionManager.findById (path is a lookup, not a pure function)", () => {
    const { cwd, sessionDir } = setup();
    const sm = SessionManager.create(cwd, sessionDir, { id: SESSION_ID });
    const file = sm.getSessionFile()!;
    sm.appendMessage({ role: "user", content: "你好", timestamp: Date.now() });

    // The timestamp prefix is assigned at creation time and is not knowable in
    // advance, so `id -> path` is NOT a pure derivation. The SDK provides an
    // exact-id lookup instead.
    expect(SessionManager.findById(cwd, SESSION_ID, sessionDir)).toBe(file);
    expect(SessionManager.findById(cwd, "no-such-session-id", sessionDir)).toBeUndefined();
  });

  it("rejects ids that are unsafe as filenames", () => {
    const { cwd, sessionDir } = setup();
    for (const bad of ["bad/id", "bad id", "..", "中文会话", "trailing-"]) {
      expect(() => SessionManager.create(cwd, sessionDir, { id: bad })).toThrow();
    }
  });
});
