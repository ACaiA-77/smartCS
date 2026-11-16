/**
 * `hardKill` must end the whole process tree, not just the child `spawn()`
 * returned.
 *
 * Why this exists: the `tsx` CLI re-execs its target as a child, so the
 * process holding a test server's port is a GRANDCHILD. Killing only the
 * direct child (what `child.kill()` does on POSIX) leaves that grandchild
 * listening; the next boot then answers `/health` from the stale process with
 * the stale script, and every later assertion is about a run the code under
 * test never produced. That is exactly how the F-matrix failed on Linux CI
 * while passing on Windows, where the kill already walked the tree.
 *
 * The port is the observable: a survivor keeps accepting connections.
 */

import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { PI_HARNESS_ROOT } from "../src/config/env.js";
import { hardKill, treeSpawnOptions, waitForPortFree } from "./helpers/harness.js";

async function freePort(): Promise<number> {
  return await new Promise<number>((resolve, reject) => {
    const server = createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 0;
      server.close(() => resolve(port));
    });
  });
}

async function portAnswers(port: number): Promise<boolean> {
  const { connect } = await import("node:net");
  return await new Promise<boolean>((resolve) => {
    const socket = connect({ port, host: "127.0.0.1" });
    socket.once("connect", () => {
      socket.destroy();
      resolve(true);
    });
    socket.once("error", () => resolve(false));
  });
}

describe("process-tree teardown", () => {
  it("hardKill ends the grandchild that holds the port, not only the tsx CLI", async () => {
    const port = await freePort();
    const tsxCli = join(PI_HARNESS_ROOT, "node_modules", "tsx", "dist", "cli.mjs");
    const child = spawn(process.execPath, [tsxCli, "tests/fixtures/kill-tree-fixture.ts", String(port)], {
      cwd: PI_HARNESS_ROOT,
      ...treeSpawnOptions(),
      stdio: ["ignore", "pipe", "pipe"],
    });

    try {
      const deadline = Date.now() + 30_000;
      while (!(await portAnswers(port))) {
        if (child.exitCode !== null) throw new Error(`fixture exited during startup (${child.exitCode})`);
        if (Date.now() > deadline) throw new Error("fixture's grandchild never started listening");
        await new Promise((r) => setTimeout(r, 100));
      }
      expect(await portAnswers(port)).toBe(true);

      await hardKill(child);

      // Throws if anything from the tree is still holding the port.
      await waitForPortFree(port, 5_000);
      expect(await portAnswers(port)).toBe(false);
    } finally {
      await waitForPortFree(port, 1_000).catch(() => undefined);
    }
  }, 60_000);
});
