/**
 * §9-进程重启 (F12) — a REAL OS process is started, killed, and restarted.
 *
 * The other acceptance cases rebuild the runtime in-process; this one spawns
 * `src/server/main.ts` as a child process, kills it with SIGKILL (no graceful
 * shutdown, no chance to flush), and starts a fresh process against the same
 * durable session directory. Nothing survives except what is on disk.
 */

import { spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { SessionManager } from "@earendil-works/pi-coding-agent";
import { signHs256 } from "../src/business/jwt-hs256.js";
import {
  PI_HARNESS_ROOT,
  pythonEnv,
  resolveSmartCsPaths,
  userJwtSecret,
  type SmartCsPaths,
} from "../src/config/env.js";
import { makeTmpDir } from "./helpers/harness.js";
import {
  TEST_DATABASE,
  childMysqlEnv,
  resetTestDatabase,
  seedAccount,
  seedSession,
  startPythonService,
  testDatabase,
  type PythonService,
  type SeededAccount,
} from "./helpers/phase1.js";
import type { Database } from "../src/db/mysql.js";

// Must be set before EITHER side starts: the Python identity server reads it at
// request time and the harness subprocess inherits it.
process.env.INTERNAL_SERVICE_JWT_SECRET =
  process.env.INTERNAL_SERVICE_JWT_SECRET ?? "phase1-restart-service-secret-0123456789";

const PY_PORT = 8_960;
const HARNESS_PORT = 8_961;

let python: PythonService;
let db: Database;
let account: SeededAccount;
let root: string;
let paths: SmartCsPaths;
let child: ChildProcess | undefined;
let stderrText = "";

const SESSION_ID = "phase1-restart-session";

function userToken(accountId: number): string {
  const now = Math.floor(Date.now() / 1000);
  return signHs256(
    { sub: String(accountId), iat: now, exp: now + 1800, iss: "smartcs", jti: `jti-${now}-${Math.random()}` },
    userJwtSecret(),
  );
}

async function startHarness(): Promise<void> {
  stderrText = "";
  // Invoke the tsx CLI through node directly: no shell, so arguments are not
  // re-parsed by cmd.exe and the child is a single killable process.
  const tsxCli = join(PI_HARNESS_ROOT, "node_modules", "tsx", "dist", "cli.mjs");
  child = spawn(process.execPath, [tsxCli, "src/server/main.ts"], {
    cwd: PI_HARNESS_ROOT,
    env: {
      ...process.env,
      ...pythonEnv(),
      // Same resolution rule as the Python fixtures (process env, then
      // python-impl/.env), so a clean shell cannot leave this child without a
      // usable platform database. See tests/README.md.
      ...childMysqlEnv(TEST_DATABASE),
      PORT: String(HARNESS_PORT),
      HOST: "127.0.0.1",
      SMARTCS_PHASE0_PROVIDER: "faux",
      SMARTCS_FAUX_CAPACITY: "50",
      SMARTCS_RUNTIME_CWD: paths.runtimeCwd,
      SMARTCS_PI_SESSION_DIR: paths.sessionDir,
      SMARTCS_PI_AGENT_DIR: paths.agentDir,
      PYTHON_INTERNAL_BASE_URL: python.url,
      INTERNAL_SERVICE_JWT_SECRET: process.env.INTERNAL_SERVICE_JWT_SECRET!,
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stderr?.on("data", (chunk) => {
    stderrText += String(chunk);
  });

  const deadline = Date.now() + 60_000;
  for (;;) {
    try {
      const response = await fetch(`http://127.0.0.1:${HARNESS_PORT}/health`, { signal: AbortSignal.timeout(1_000) });
      if (response.ok) return;
    } catch {
      /* not up yet */
    }
    if (Date.now() > deadline) throw new Error(`harness did not start: ${stderrText}`);
    await new Promise((r) => setTimeout(r, 200));
  }
}

/** Hard kill: no SIGTERM handler runs, so nothing gets a chance to flush. */
async function killHarness(): Promise<void> {
  if (!child) return;
  const dying = child;
  child = undefined;
  await new Promise<void>((resolve) => {
    dying.once("exit", () => resolve());
    if (process.platform === "win32") {
      spawn("taskkill", ["/pid", String(dying.pid), "/f", "/t"], { stdio: "ignore" });
    } else {
      dying.kill("SIGKILL");
    }
    setTimeout(resolve, 10_000).unref?.();
  });
  // Let the port be released before rebinding.
  await new Promise((r) => setTimeout(r, 500));
}

async function chat(clientRequestId: string, message: string) {
  const response = await fetch(`http://127.0.0.1:${HARNESS_PORT}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${userToken(account.accountId)}` },
    body: JSON.stringify({ session_id: SESSION_ID, client_request_id: clientRequestId, message }),
  });
  return { status: response.status, body: (await response.json()) as Record<string, unknown> };
}

function entryCount(): number {
  const found = SessionManager.findById(paths.runtimeCwd, SESSION_ID, paths.sessionDir);
  if (!found || !existsSync(found)) return 0;
  return SessionManager.open(found, paths.sessionDir, paths.runtimeCwd).getEntries().length;
}

describe("§9 process restart through a real OS process (F12)", () => {
  beforeAll(async () => {
    await resetTestDatabase();
    db = testDatabase();
    account = await seedAccount(db, { username: "restart-owner", businessUserId: "bu-restart" });
    await seedSession(db, { sessionId: SESSION_ID, accountId: account.accountId, harnessVersion: "pi" });

    python = await startPythonService({ port: PY_PORT });

    root = makeTmpDir("phase1-restart-");
    paths = resolveSmartCsPaths({
      runtimeCwd: join(root, "cwd"),
      sessionDir: join(root, "sessions"),
      agentDir: join(root, "agent"),
    });
    mkdirSync(paths.runtimeCwd, { recursive: true });
    mkdirSync(paths.sessionDir, { recursive: true });

    await startHarness();
  }, 180_000);

  afterAll(async () => {
    await killHarness();
    await python?.stop();
    await db?.close();
    try {
      rmSync(root, { recursive: true, force: true });
    } catch {
      /* disposable */
    }
  });

  it("kills the process mid-life and continues the same transcript afterwards", async () => {
    const first = await chat("restart-req-1", "重启前的第一条消息");
    expect(first.status).toBe(200);
    expect(String((first.body.message as { content: string }).content)).toContain("重启前的第一条消息");

    const before = entryCount();
    expect(before).toBeGreaterThan(0);

    await killHarness();
    // The transcript must already be durable on disk with no graceful shutdown.
    expect(entryCount()).toBe(before);

    await startHarness();

    const second = await chat("restart-req-2", "重启后的第二条消息");
    expect(second.status).toBe(200);
    expect(String((second.body.message as { content: string }).content)).toContain("重启后的第二条消息");

    // The reopened runtime appended to the SAME file, so the earlier turn is
    // still there and the transcript grew rather than restarting.
    expect(entryCount()).toBeGreaterThan(before);

    const found = SessionManager.findById(paths.runtimeCwd, SESSION_ID, paths.sessionDir)!;
    const manager = SessionManager.open(found, paths.sessionDir, paths.runtimeCwd);
    const context = JSON.stringify(manager.buildSessionContext().messages);
    expect(context).toContain("重启前的第一条消息");
    expect(context).toContain("重启后的第二条消息");
    expect(manager.getSessionId()).toBe(SESSION_ID);
  }, 180_000);

  it("does not replay the pre-crash request twice after the restart", async () => {
    // The same client_request_id as before the crash: the receipt survived in
    // MySQL, so this must replay rather than append another turn.
    const before = entryCount();
    const replay = await chat("restart-req-1", "重启前的第一条消息");
    expect(replay.status).toBe(200);
    expect(replay.body.replayed).toBe(true);
    expect(entryCount()).toBe(before);
  }, 60_000);
});
