/**
 * A5 — What happens when the same session id is used by two processes?
 *
 * Plan v2 §6.2 assumes a single pi-harness instance plus a per-session actor
 * queue; A5 is the safety net for that assumption ("单实例假设的保护网").
 * SessionManager takes no lock (no proper-lockfile usage in session-manager),
 * so the SDK itself provides no cross-process mutual exclusion.
 *
 * Two deterministic facts are asserted here; the timing-dependent collision is
 * probed and classified rather than assumed.
 */

import { execFile } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";
import { SessionManager } from "@earendil-works/pi-coding-agent";
import { makeTmpDir } from "./helpers/harness.js";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const WORKER = join(HERE, "fixtures", "session-worker.mjs");

interface WorkerResult {
  ok: boolean;
  pid?: number;
  id?: string;
  file?: string;
  openedExisting?: boolean;
  error?: string;
  code?: string;
}

function runWorker(args: (string | number)[]): Promise<WorkerResult> {
  return new Promise((resolve) => {
    execFile(process.execPath, [WORKER, ...args.map(String)], { timeout: 30_000 }, (_err, stdout) => {
      const line = String(stdout).trim().split("\n").filter(Boolean).at(-1);
      if (!line) {
        resolve({ ok: false, error: "worker produced no output" });
        return;
      }
      try {
        resolve(JSON.parse(line) as WorkerResult);
      } catch {
        resolve({ ok: false, error: `unparseable output: ${line}` });
      }
    });
  });
}

function paths() {
  const root = makeTmpDir("smartcs-a5-");
  const cwd = join(root, "cwd");
  const sessionDir = join(root, "sessions");
  return { root, cwd, sessionDir };
}

const countUserMessages = (file: string) =>
  readFileSync(file, "utf-8")
    .split("\n")
    .filter(Boolean)
    .map((l) => JSON.parse(l) as { type: string; message?: { role?: string } })
    .filter((e) => e.type === "message" && e.message?.role === "user").length;

describe("A5 two processes, one session id", () => {
  it("two sequential create() calls with the same id produce TWO files sharing that id", async () => {
    const { cwd, sessionDir } = paths();
    const id = "smartcs-a5-split";

    const first = await runWorker(["create", id, sessionDir, cwd, "first"]);
    // A distinct millisecond guarantees a distinct timestamp prefix.
    await new Promise((r) => setTimeout(r, 5));
    const second = await runWorker(["create", id, sessionDir, cwd, "second"]);

    expect(first.ok).toBe(true);
    expect(second.ok).toBe(true);
    expect(first.id).toBe(id);
    expect(second.id).toBe(id);

    // DELTA vs plan §5.2: the id is NOT unique per directory. "create with a
    // known id" is not idempotent — it silently forks a second transcript.
    expect(first.file).not.toBe(second.file);
    expect(existsSync(first.file!)).toBe(true);
    expect(existsSync(second.file!)).toBe(true);
    expect(countUserMessages(first.file!)).toBe(1);
    expect(countUserMessages(second.file!)).toBe(1);
  });

  it("SAFETY NET: a second process can open the first process's file and append without losing history", async () => {
    const { cwd, sessionDir } = paths();
    const id = "smartcs-a5-handoff";

    const creator = await runWorker(["create", id, sessionDir, cwd, "from creator"]);
    expect(creator.ok).toBe(true);

    const opener = await runWorker(["open", id, sessionDir, cwd, "from opener"]);
    expect(opener.ok).toBe(true);
    expect(opener.file).toBe(creator.file);
    expect(opener.openedExisting).toBe(true);

    // append-only: both writes survive, file remains valid JSONL
    const file = creator.file!;
    expect(countUserMessages(file)).toBe(2);
    expect(readFileSync(file, "utf-8")).toContain("from creator");
    expect(readFileSync(file, "utf-8")).toContain("from opener");
  });

  it("HAZARD: same-millisecond same-id create makes the second writer fail at first append", () => {
    // Freeze the clock so both managers derive the identical filename. This is
    // a deterministic stand-in for "two processes created in the same ms".
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
      const { cwd, sessionDir } = paths();
      const id = "smartcs-a5-collide";

      const a = SessionManager.create(cwd, sessionDir, { id });
      const b = SessionManager.create(cwd, sessionDir, { id });
      expect(a.getSessionFile()).toBe(b.getSessionFile());

      // First writer takes the file with an exclusive create.
      a.appendMessage({ role: "user", content: "from a", timestamp: Date.now() });
      expect(existsSync(a.getSessionFile()!)).toBe(true);

      // Second writer hits openSync(path, "wx") -> EEXIST. There is no lock,
      // no retry, no merge: the append throws.
      let thrown: NodeJS.ErrnoException | undefined;
      try {
        b.appendMessage({ role: "user", content: "from b", timestamp: Date.now() });
      } catch (error) {
        thrown = error as NodeJS.ErrnoException;
      }
      expect(thrown).toBeDefined();
      expect(thrown!.code).toBe("EEXIST");

      // The surviving file must contain exactly the first writer's message.
      expect(countUserMessages(a.getSessionFile()!)).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("PROBE: simultaneous same-id create is classified, never silently corrupting", async () => {
    const { cwd, sessionDir } = paths();
    const id = "smartcs-a5-race";
    // Aim both workers at the same wall-clock millisecond so the derived
    // filename collides.
    const barrier = Date.now() + 300;

    const [a, b] = await Promise.all([
      runWorker(["create", id, sessionDir, cwd, "race-a", barrier]),
      runWorker(["create", id, sessionDir, cwd, "race-b", barrier]),
    ]);

    const outcomes = { sameFile: false, splitFiles: false, error: false, unknown: false };
    if (!a.ok || !b.ok) outcomes.error = true;
    else if (a.file === b.file) outcomes.sameFile = true;
    else if (a.file && b.file) outcomes.splitFiles = true;
    else outcomes.unknown = true;

    // Whatever the race resolves to, it must be one of the classified shapes —
    // and when both succeeded on one file the file must hold both messages.
    expect(outcomes.unknown).toBe(false);
    expect(outcomes.sameFile || outcomes.splitFiles || outcomes.error).toBe(true);

    if (outcomes.sameFile) {
      expect(countUserMessages(a.file!)).toBe(2);
    }

    // Record the observed classification for the Phase 0 report.
    console.log("[A5 race probe]", JSON.stringify({ a, b, outcomes }));
  });
});
