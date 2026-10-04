/**
 * SessionRegistry: per-session serialisation (F8), bounded queue, timeouts,
 * idle eviction, and the 先查后建 discipline (D4).
 */

import { existsSync, mkdirSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import type { AgentSession } from "@earendil-works/pi-coding-agent";
import { SessionRegistry, SessionBusyError, SessionQueueFullError } from "../src/session/registry.js";
import { openOrCreateSessionManager } from "../src/session/pi-session.js";
import { makeTmpDir } from "./helpers/harness.js";

/** Minimal stand-in for AgentSession; the registry only disposes/stores it. */
function fakeSession(): AgentSession & { disposed: boolean } {
  const session = {
    disposed: false,
    dispose() {
      session.disposed = true;
    },
  };
  return session as unknown as AgentSession & { disposed: boolean };
}

function fakeRegistry(options = {}) {
  const created: string[] = [];
  const registry = new SessionRegistry(async (sessionId) => {
    created.push(sessionId);
    return { session: fakeSession(), origin: "created" as const };
  }, options);
  return { registry, created };
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

describe("SessionRegistry", () => {
  it("serialises same-session requests: inFlight never exceeds 1 and order is deterministic", async () => {
    const { registry } = fakeRegistry();
    const order: string[] = [];
    let maxConcurrent = 0;
    let concurrent = 0;

    const task = (name: string, holdMs: number) =>
      registry.withExclusive("s1", async () => {
        concurrent += 1;
        maxConcurrent = Math.max(maxConcurrent, concurrent);
        order.push(`start:${name}`);
        await sleep(holdMs);
        order.push(`end:${name}`);
        concurrent -= 1;
      });

    await Promise.all([task("a", 40), task("b", 5), task("c", 5)]);

    expect(maxConcurrent).toBe(1);
    // Strict FIFO: a finishes before b starts, b before c.
    expect(order).toEqual(["start:a", "end:a", "start:b", "end:b", "start:c", "end:c"]);
  });

  it("does not serialise different sessions", async () => {
    const { registry } = fakeRegistry();
    let concurrent = 0;
    let maxConcurrent = 0;
    const task = () =>
      registry.withExclusive("s", async () => {
        concurrent += 1;
        maxConcurrent = Math.max(maxConcurrent, concurrent);
        await sleep(30);
        concurrent -= 1;
      });
    await Promise.all([task(), task(), task()]);
    expect(maxConcurrent).toBe(1);

    const across: string[] = [];
    await Promise.all([
      registry.withExclusive("a", async () => {
        across.push("a");
        await sleep(20);
      }),
      registry.withExclusive("b", async () => {
        across.push("b");
        await sleep(20);
      }),
    ]);
    expect(across.sort()).toEqual(["a", "b"]);
  });

  it("rejects a waiter that exceeds the acquire timeout", async () => {
    const { registry } = fakeRegistry();
    const lease = await registry.acquire("slow");
    await expect(registry.acquire("slow", 50)).rejects.toBeInstanceOf(SessionBusyError);
    lease.release();
  });

  it("rejects new arrivals when the per-session queue is full", async () => {
    const { registry } = fakeRegistry({ maxQueuePerSession: 1, acquireTimeoutMs: 5_000 });
    const lease = await registry.acquire("q");
    const queued = registry.acquire("q"); // occupies the single queue slot
    await expect(registry.acquire("q")).rejects.toBeInstanceOf(SessionQueueFullError);
    lease.release();
    await queued;
  });

  it("tryAcquire refuses instead of queueing (DELETE-history path)", async () => {
    const { registry } = fakeRegistry();
    const lease = await registry.acquire("busy");
    expect(registry.tryAcquire("busy")).toBeUndefined();
    lease.release();
    const free = registry.tryAcquire("busy");
    expect(free).toBeDefined();
    free!.release();
  });

  it("idle eviction disposes the session and forgets it, without saving", async () => {
    const { registry } = fakeRegistry();
    const { session } = await registry.ensureSession("evict-me");
    expect(registry.get("evict-me")).toBeDefined();

    registry.scheduleIdleEviction("evict-me", 10);
    await sleep(80);

    expect((session as unknown as { disposed: boolean }).disposed).toBe(true);
    expect(registry.get("evict-me")).toBeUndefined();
    expect(registry.stats().evicted).toBe(1);
  });

  it("idle eviction is postponed while the session is busy", async () => {
    const { registry } = fakeRegistry();
    const { session } = await registry.ensureSession("busy-evict");
    const lease = await registry.acquire("busy-evict");

    registry.scheduleIdleEviction("busy-evict", 10);
    await sleep(80);
    // Still alive: a run is in flight.
    expect(registry.get("busy-evict")).toBeDefined();
    expect((session as unknown as { disposed: boolean }).disposed).toBe(false);

    lease.release();
    await sleep(1_300); // eviction retries on a 1s timer
    expect((session as unknown as { disposed: boolean }).disposed).toBe(true);
  });
});

describe("先查后建 (D4 discipline)", () => {
  it("two acquires of one session id produce exactly ONE session file", async () => {
    const root = makeTmpDir("phase1-lookup-");
    const paths = {
      runtimeCwd: join(root, "cwd"),
      sessionDir: join(root, "sessions"),
      agentDir: join(root, "agent"),
    };
    mkdirSync(paths.runtimeCwd, { recursive: true });
    mkdirSync(paths.sessionDir, { recursive: true });

    const first = openOrCreateSessionManager(paths, "smartcs-lookup");
    expect(first.origin).toBe("created");
    first.sessionManager.appendMessage({ role: "user", content: "你好", timestamp: Date.now() });

    const second = openOrCreateSessionManager(paths, "smartcs-lookup");
    expect(second.origin).toBe("opened");
    expect(second.file).toBe(first.file);
    expect(second.sessionManager.getSessionId()).toBe("smartcs-lookup");

    const files = readdirSync(paths.sessionDir).filter((f) => f.endsWith(".jsonl"));
    expect(files).toHaveLength(1);
    expect(existsSync(first.file!)).toBe(true);
  });

  it("creates a fresh session when no file exists for the id", () => {
    const root = makeTmpDir("phase1-lookup-new-");
    const paths = {
      runtimeCwd: join(root, "cwd"),
      sessionDir: join(root, "sessions"),
      agentDir: join(root, "agent"),
    };
    const resolved = openOrCreateSessionManager(paths, "smartcs-brand-new");
    expect(resolved.origin).toBe("created");
    expect(resolved.sessionManager.getSessionId()).toBe("smartcs-brand-new");
  });
});
