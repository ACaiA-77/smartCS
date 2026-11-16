/**
 * Shared test helpers.
 *
 * All tests run against the Faux provider (no network, no real LLM, no
 * Python endpoint) unless a test explicitly opts into the real provider.
 */

import { spawn, type ChildProcess } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { connect } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { registerFauxProvider, type FauxProviderRegistration } from "@earendil-works/pi-ai/compat";
import type { FauxResponseStep } from "@earendil-works/pi-ai/providers/faux";
import { createSmartCsAgent, type CreateSmartCsAgentOptions, type SmartCsAgentHandle } from "../../src/agent/create-smartcs-agent.js";

export function makeTmpDir(prefix = "smartcs-phase0-"): string {
  return mkdtempSync(join(tmpdir(), prefix));
}

/**
 * Spawn options for a server the tests will later hard-kill.
 *
 * `tsx` re-execs the target as its own child, so the process actually holding
 * the listening port is a GRANDCHILD of the one `spawn()` returned. Killing
 * only that direct child (what `child.kill()` does on POSIX) leaves the
 * grandchild alive and listening; the next boot's `/health` probe then answers
 * from the stale process, and every later assertion is about the wrong run.
 * Leading a process group of its own makes the whole tree killable.
 */
export function treeSpawnOptions(): { detached: boolean } {
  // Windows gets no new console: `taskkill /T` already walks the tree there.
  return { detached: process.platform !== "win32" };
}

/**
 * SIGKILL the whole tree with no graceful shutdown path — nothing gets a
 * chance to flush. Resolves once the direct child has exited (the group kill
 * is issued first, so the grandchild is gone by then on POSIX).
 */
export function hardKill(child: ChildProcess): Promise<void> {
  return new Promise<void>((resolve) => {
    if (child.exitCode !== null || child.signalCode !== null) {
      resolve();
      return;
    }
    child.once("exit", () => resolve());
    if (process.platform === "win32") {
      spawn("taskkill", ["/pid", String(child.pid), "/f", "/t"], { stdio: "ignore" });
    } else {
      try {
        // Negative pid = the whole process group created by `treeSpawnOptions`.
        if (child.pid) process.kill(-child.pid, "SIGKILL");
        else child.kill("SIGKILL");
      } catch {
        child.kill("SIGKILL");
      }
    }
    setTimeout(resolve, 10_000).unref?.();
  });
}

/**
 * Refuse to continue until the port is genuinely free.
 *
 * A restart test that rebinds the same port while a survivor still answers
 * `/health` would silently attach the new test to the old process, and the
 * failure would surface as an assertion about a response the code under test
 * never produced. Failing here names the real defect instead.
 */
export async function waitForPortFree(port: number, timeoutMs = 10_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const reachable = await new Promise<boolean>((resolve) => {
      const socket = connect({ port, host: "127.0.0.1" });
      socket.once("connect", () => {
        socket.destroy();
        resolve(true);
      });
      socket.once("error", () => resolve(false));
      socket.setTimeout(500, () => {
        socket.destroy();
        resolve(false);
      });
    });
    if (!reachable) return;
    if (Date.now() > deadline) {
      throw new Error(
        `port ${port} is still accepting connections after the hard kill: a previous harness survived it`,
      );
    }
    await new Promise((r) => setTimeout(r, 100));
  }
}

export interface TestAgentHandle {
  agent: SmartCsAgentHandle;
  faux: FauxProviderRegistration;
  /** Temp root holding cwd/sessions/agent dirs. */
  root: string;
  /** Release runtime resources but KEEP the session files (reopen tests). */
  disposeSession: () => void;
  /** Release runtime resources and delete the temp root. */
  cleanup: () => void;
}

/**
 * Create a SmartCS agent backed by a fresh Faux provider and an isolated temp
 * session dir. Every call gets its own provider registration and its own
 * on-disk session root, so assertions cannot pass by leaking state.
 */
export interface TestAgentOptions extends Omit<CreateSmartCsAgentOptions, "faux" | "provider"> {
  /** Override the faux model definition (e.g. a tiny contextWindow to force compaction). */
  fauxModel?: { id?: string; contextWindow?: number; maxTokens?: number };
}

export async function createTestAgent(
  responses: FauxResponseStep[],
  options: TestAgentOptions = {},
): Promise<TestAgentHandle> {
  const { fauxModel, ...agentOptions } = options;
  const faux = registerFauxProvider({
    provider: "faux",
    api: "faux",
    models: [{ id: fauxModel?.id ?? "faux-1", name: "Faux 1", contextWindow: fauxModel?.contextWindow, maxTokens: fauxModel?.maxTokens }],
  });
  faux.setResponses(responses);

  const root = makeTmpDir();
  const agent = await createSmartCsAgent({
    provider: "faux",
    faux,
    // Phase 0/1 SDK-level tests use the hard-coded tool doubles; Phase 2
    // acceptance opts into "business" explicitly.
    toolMode: "fake",
    paths: {
      runtimeCwd: join(root, "cwd"),
      sessionDir: join(root, "sessions"),
      agentDir: join(root, "agent"),
    },
    ...agentOptions,
  });

  let disposed = false;
  const disposeSession = () => {
    if (disposed) return;
    disposed = true;
    agent.dispose();
    faux.unregister();
  };

  return {
    agent,
    faux,
    root,
    disposeSession,
    cleanup: () => {
      disposeSession();
      try {
        rmSync(root, { recursive: true, force: true });
      } catch {
        /* Windows can hold a handle briefly; temp dirs are disposable. */
      }
    },
  };
}
