/**
 * Phase 10 §① acceptance — the memory outbox as a PRODUCTION loop.
 *
 * The chain `receipt(pending) → MemoryOutboxDispatcher → /internal/memory/enqueue
 * → UserMemoryService` existed since Phase 3 but had no producer: `main.ts`
 * never started the dispatcher, and the dispatcher's identity came from the
 * in-process `TurnContext`, which is gone the moment it would be needed.
 *
 * These cases pin the replacement contract:
 *
 *   P10-1  a completed request eventually becomes `done`               (in-process)
 *   P10-2  the production entrypoint delivers after a hard crash        (REAL process)
 *   P10-3  delivery survives an idle eviction                           (in-process)
 *   P10-4  duplicate delivery creates no duplicate candidate            (in-process)
 *   P10-5  a delivery failure retries memory only — never LLM or tools  (in-process)
 *
 * Everything except the model is real: real MySQL, real Python `internal_api`
 * in a subprocess, real Pi session files. P10-2 additionally runs the real
 * `src/server/main.ts` and kills it with a crash point, because the whole point
 * of the outbox is what survives a process that died at the wrong moment.
 */

import { spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import type { RowDataPacket } from "mysql2/promise";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { fauxAssistantMessage, fauxText, fauxToolCall } from "@earendil-works/pi-ai/providers/faux";
import type { FauxProviderRegistration } from "@earendil-works/pi-ai/compat";
import { createFauxProvider } from "../src/agent/create-smartcs-agent.js";
import { SessionRegistry } from "../src/session/registry.js";
import { openOrCreatePiSession } from "../src/session/pi-session.js";
import { MemoryOutboxDispatcher } from "../src/session/outbox.js";
import { PythonInternalClient } from "../src/business/python-client.js";
import { ReceiptStore } from "../src/db/receipts.js";
import { MemorySourceStore } from "../src/db/memory-source.js";
import { createHarnessServer, type HarnessServer } from "../src/server/app.js";
import { signHs256 } from "../src/business/jwt-hs256.js";
import { PI_HARNESS_ROOT, pythonEnv, resolveSmartCsPaths, userJwtSecret, type SmartCsPaths } from "../src/config/env.js";
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

process.env.INTERNAL_SERVICE_JWT_SECRET =
  process.env.INTERNAL_SERVICE_JWT_SECRET ?? "phase10-outbox-service-secret-0123456789";

const PY_PORT = 8_974;
const HARNESS_PORT = 8_975;
const CRASH_HARNESS_PORT = 8_976;
const SESSION_ID = "phase10-outbox-session";
const CRASH_SESSION_ID = "phase10-outbox-crash-session";

let python: PythonService;
let db: Database;
let receipts: ReceiptStore;
let memorySource: MemorySourceStore;
let client: PythonInternalClient;
let root: string;
let crashRoot: string;
let paths: SmartCsPaths;
let crashPaths: SmartCsPaths;
let faux: FauxProviderRegistration;
let harness: HarnessServer;
let registry: SessionRegistry;
let account: SeededAccount;
let harnessPort = HARNESS_PORT;

function userToken(accountId: number): string {
  const now = Math.floor(Date.now() / 1000);
  return signHs256(
    { sub: String(accountId), iat: now, exp: now + 1800, iss: "smartcs", jti: `jti-${now}-${Math.random()}` },
    userJwtSecret(),
  );
}

async function buildHarness(idleEvictionMs = 60 * 60_000): Promise<HarnessServer> {
  registry = new SessionRegistry(async (sessionId) => {
    const handle = await openOrCreatePiSession(paths, sessionId, {
      provider: "faux",
      faux,
      toolMode: "business",
      pythonClient: client,
    });
    return {
      session: handle.session,
      origin: handle.origin,
      turnContext: handle.turnContext,
      snapshotHolder: handle.snapshotHolder,
    };
  });
  const server = createHarnessServer({
    paths,
    registry,
    receipts,
    memorySource,
    pythonClient: client,
    idleEvictionMs,
  });
  harnessPort = HARNESS_PORT;
  await server.listen(harnessPort);
  return server;
}

async function chat(clientRequestId: string, message: string) {
  const response = await fetch(`http://127.0.0.1:${harnessPort}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${userToken(account.accountId)}` },
    body: JSON.stringify({ session_id: SESSION_ID, client_request_id: clientRequestId, message }),
  });
  return { status: response.status, body: (await response.json()) as Record<string, unknown> };
}

/** Candidates the runtime actually queued for one provenance event. */
async function candidateCount(sourceEventId: string): Promise<number> {
  const rows = await db.query<RowDataPacket>(
    "SELECT COUNT(*) AS n FROM user_memory_candidate WHERE source_event_id = ?",
    [sourceEventId],
  );
  return Number(rows[0]?.n ?? 0);
}

async function sourceEventFor(clientRequestId: string): Promise<string> {
  const eventId = await receipts.sourceEventId(SESSION_ID, clientRequestId);
  expect(eventId, "the pipeline must have recorded provenance").toBeTruthy();
  return eventId!;
}

async function waitFor<T>(probe: () => Promise<T | undefined>, timeoutMs: number, label: string): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = await probe();
    if (value !== undefined) return value;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${label}`);
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
}

// --- the real-process half (P10-2) -------------------------------------------

let crashChild: ChildProcess | undefined;
let crashStderr = "";
let markerFile = "";

function startCrashHarness(options: { crashPoint?: boolean }): void {
  crashStderr = "";
  const tsxCli = join(PI_HARNESS_ROOT, "node_modules", "tsx", "dist", "cli.mjs");
  crashChild = spawn(process.execPath, [tsxCli, "src/server/main.ts"], {
    cwd: PI_HARNESS_ROOT,
    env: {
      ...process.env,
      ...pythonEnv(),
      ...childMysqlEnv(TEST_DATABASE),
      PORT: String(CRASH_HARNESS_PORT),
      HOST: "127.0.0.1",
      // The explicit offline request — never an inferred fallback (Phase 10 §④).
      SMARTCS_PROVIDER_MODE: "faux",
      SMARTCS_FAUX_CAPACITY: "50",
      SMARTCS_RUNTIME_CWD: crashPaths.runtimeCwd,
      SMARTCS_PI_SESSION_DIR: crashPaths.sessionDir,
      SMARTCS_PI_AGENT_DIR: crashPaths.agentDir,
      PYTHON_INTERNAL_BASE_URL: python.url,
      INTERNAL_SERVICE_JWT_SECRET: process.env.INTERNAL_SERVICE_JWT_SECRET!,
      SMARTCS_IDLE_EVICTION_MS: "600000",
      // Small on purpose: the dispatcher must reach the crash window while the
      // test is still watching.
      SMARTCS_MEMORY_INTERVAL_MS: "300",
      ...(options.crashPoint
        ? { SMARTCS_CRASH_POINT: "before_memory_enqueue", SMARTCS_CRASH_MARKER: markerFile }
        : {}),
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  crashChild.stderr?.on("data", (chunk) => {
    crashStderr += String(chunk);
  });
}

async function waitForCrashHarness(): Promise<void> {
  const deadline = Date.now() + 60_000;
  for (;;) {
    if (crashChild?.exitCode !== null && crashChild?.exitCode !== undefined) {
      throw new Error(`harness exited during startup (code=${crashChild.exitCode}): ${crashStderr}`);
    }
    try {
      const response = await fetch(`http://127.0.0.1:${CRASH_HARNESS_PORT}/health`, {
        signal: AbortSignal.timeout(1_000),
      });
      if (response.ok) return;
    } catch {
      /* not up yet */
    }
    if (Date.now() > deadline) throw new Error(`harness did not start: ${crashStderr}`);
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
}

async function killCrashHarness(): Promise<void> {
  if (!crashChild) return;
  const dying = crashChild;
  crashChild = undefined;
  await new Promise<void>((resolve) => {
    dying.once("exit", () => resolve());
    if (process.platform === "win32") {
      spawn("taskkill", ["/pid", String(dying.pid), "/f", "/t"], { stdio: "ignore" });
    } else {
      dying.kill("SIGKILL");
    }
    setTimeout(resolve, 10_000).unref?.();
  });
  await new Promise((resolve) => setTimeout(resolve, 500));
}

async function crashChat(clientRequestId: string, message: string) {
  try {
    const response = await fetch(`http://127.0.0.1:${CRASH_HARNESS_PORT}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${userToken(account.accountId)}` },
      body: JSON.stringify({ session_id: CRASH_SESSION_ID, client_request_id: clientRequestId, message }),
    });
    return { status: response.status, body: (await response.json()) as Record<string, unknown> };
  } catch (error) {
    // The crash point can abort the process before the response is flushed —
    // which is itself part of what is being asserted.
    return { status: 0, body: {} as Record<string, unknown>, error: String(error) };
  }
}

describe("Phase 10 §① memory outbox production loop", () => {
  beforeAll(async () => {
    await resetTestDatabase();
    db = testDatabase();
    account = await seedAccount(db, { username: "phase10-owner", businessUserId: "user_001" });
    await seedSession(db, { sessionId: SESSION_ID, accountId: account.accountId, harnessVersion: "pi" });
    await seedSession(db, { sessionId: CRASH_SESSION_ID, accountId: account.accountId, harnessVersion: "pi" });

    python = await startPythonService({ port: PY_PORT });
    receipts = new ReceiptStore(db);
    memorySource = new MemorySourceStore(db);
    client = new PythonInternalClient({ baseUrl: python.url });

    root = makeTmpDir("phase10-outbox-");
    paths = resolveSmartCsPaths({
      runtimeCwd: join(root, "cwd"),
      sessionDir: join(root, "sessions"),
      agentDir: join(root, "agent"),
    });
    crashRoot = makeTmpDir("phase10-outbox-crash-");
    crashPaths = resolveSmartCsPaths({
      runtimeCwd: join(crashRoot, "cwd"),
      sessionDir: join(crashRoot, "sessions"),
      agentDir: join(crashRoot, "agent"),
    });
    markerFile = join(crashRoot, "crash-marker.json");
    for (const dir of [paths.runtimeCwd, paths.sessionDir, crashPaths.runtimeCwd, crashPaths.sessionDir]) {
      mkdirSync(dir, { recursive: true });
    }

    faux = createFauxProvider();
    faux.setResponses(Array.from({ length: 40 }, () => (context: { messages: Array<{ role?: string; content?: unknown }> }) => {
      const textOf = (content: unknown): string => {
        if (typeof content === "string") return content;
        if (!Array.isArray(content)) return "";
        return content.map((block) => String((block as { text?: string }).text ?? "")).join("");
      };
      const lastUser = [...context.messages]
        .reverse()
        .find((m) => m.role === "user" && !textOf(m.content).startsWith("[SmartCS 业务上下文快照"));
      return fauxAssistantMessage(`[faux] 收到：${textOf(lastUser?.content)}`);
    }));

    harness = await buildHarness();
  }, 240_000);

  afterAll(async () => {
    await killCrashHarness();
    await harness?.close();
    faux?.unregister();
    await python?.stop();
    await db?.close();
    for (const dir of [root, crashRoot]) {
      try {
        rmSync(dir, { recursive: true, force: true });
      } catch {
        /* disposable */
      }
    }
  });

  it("P10-1: a completed request is delivered and the receipt becomes done", async () => {
    const response = await chat("p10-1", "你好，我的订单在哪里？");
    expect(response.status).toBe(200);

    const before = await receipts.memoryState(SESSION_ID, "p10-1");
    expect(before?.status).toBe("pending");

    const summary = await new MemoryOutboxDispatcher({ receipts, pythonClient: client }).dispatchOnce();
    expect(summary.enqueued).toBeGreaterThan(0);
    expect(summary.failed).toBe(0);

    const after = await receipts.memoryState(SESSION_ID, "p10-1");
    expect(after?.status).toBe("done");
  }, 120_000);

  it("P10-3: delivery works after the session was idle-evicted", async () => {
    const response = await chat("p10-3", "以后请用中文回复我");
    expect(response.status).toBe(200);
    expect(registry.get(SESSION_ID), "session must be resident right after the turn").toBeDefined();

    // Drive the real eviction path (dispose + forget) rather than racing a
    // short timer: `scheduleIdleEviction` with a zero delay runs the same
    // `evictIfIdle` the production idle window runs.
    registry.scheduleIdleEviction(SESSION_ID, 0);
    await waitFor(
      async () => (registry.get(SESSION_ID) === undefined ? true : undefined),
      10_000,
      "idle eviction",
    );

    // Nothing in this process holds the identity any more — which is precisely
    // the state the old `identityFor(sessionId)` implementation could never
    // recover from. The dispatcher must rebuild it from durable rows.
    const summary = await new MemoryOutboxDispatcher({ receipts, pythonClient: client }).dispatchOnce();
    expect(summary.enqueued).toBeGreaterThan(0);
    expect((await receipts.memoryState(SESSION_ID, "p10-3"))?.status).toBe("done");
  }, 180_000);

  it("P10-4: a duplicate delivery creates no duplicate candidate", async () => {
    const response = await chat("p10-4", "以后请用中文回复我，谢谢");
    expect(response.status).toBe(200);

    const sourceEventId = await sourceEventFor("p10-4");
    const dispatcher = new MemoryOutboxDispatcher({ receipts, pythonClient: client });
    await dispatcher.dispatchOnce();
    expect((await receipts.memoryState(SESSION_ID, "p10-4"))?.status).toBe("done");

    const first = await candidateCount(sourceEventId);
    expect(first, "this message must actually produce a candidate, or the case proves nothing").toBeGreaterThan(0);

    // Simulate the delivery being attempted twice — a lost response, or a
    // second process that scanned the same row before the CAS landed. The
    // receipt is forced back to `pending`; the runtime must absorb the replay.
    await db.execute(
      "UPDATE agent_run_receipt SET memory_enqueue_status='pending' WHERE session_id = ? AND client_request_id = ?",
      [SESSION_ID, "p10-4"],
    );
    const second = await dispatcher.dispatchOnce();
    expect(second.enqueued).toBeGreaterThan(0);

    expect(await candidateCount(sourceEventId)).toBe(first);
    expect((await receipts.memoryState(SESSION_ID, "p10-4"))?.status).toBe("done");
  }, 120_000);

  it("P10-5: a failed delivery retries memory only — the model and tools never rerun", async () => {
    // A tool call, so a replay would be visible as a second tool invocation.
    faux.setResponses([
      fauxAssistantMessage(
        [fauxText("我查一下。"), fauxToolCall("order_query", { order_id: "ORD-20260801-0001" })],
        { stopReason: "toolUse" },
      ),
      fauxAssistantMessage("已查到您的订单。"),
    ]);
    const response = await chat("p10-5", "帮我查一下订单 ORD-20260801-0001");
    expect(response.status).toBe(200);

    const session = registry.get(SESSION_ID)!;
    const transcriptBefore = JSON.stringify(session.session.sessionManager.getEntries());
    const receiptBefore = await receipts.get(
      (await db.query<RowDataPacket>(
        "SELECT id FROM agent_run_receipt WHERE session_id = ? AND client_request_id = ?",
        [SESSION_ID, "p10-5"],
      ))[0]!.id as number,
    );

    // A runtime that cannot be reached: delivery fails, and must keep failing
    // as *memory*, without touching the receipt's status or the transcript.
    const unreachable = new MemoryOutboxDispatcher({
      receipts,
      pythonClient: new PythonInternalClient({ baseUrl: "http://127.0.0.1:1", timeoutMs: 500 }),
    });
    const failed = await unreachable.dispatchOnce();
    expect(failed.failed).toBeGreaterThan(0);
    expect(failed.enqueued).toBe(0);

    const state = await receipts.memoryState(SESSION_ID, "p10-5");
    expect(state?.status).toBe("pending");
    expect(state!.attempts).toBeGreaterThan(0);

    // The run itself is untouched: same status, same response, same transcript.
    const receiptAfter = await receipts.get(receiptBefore!.id);
    expect(receiptAfter?.status).toBe("completed");
    expect(JSON.stringify(receiptAfter?.response)).toBe(JSON.stringify(receiptBefore?.response));
    expect(JSON.stringify(session.session.sessionManager.getEntries())).toBe(transcriptBefore);

    // And the retry succeeds on the next pass once the runtime is reachable.
    const recovered = await new MemoryOutboxDispatcher({ receipts, pythonClient: client }).dispatchOnce();
    expect(recovered.enqueued).toBeGreaterThan(0);
    expect((await receipts.memoryState(SESSION_ID, "p10-5"))?.status).toBe("done");
  }, 180_000);

  it("P10-2: the production entrypoint delivers a receipt left pending by a hard crash", async () => {
    // The real `src/server/main.ts`, with the real dispatcher started from it —
    // armed to abort the process at the exact window the outbox exists for.
    startCrashHarness({ crashPoint: true });
    await waitForCrashHarness();

    // The message carries an explicit preference so the runtime has something
    // to extract; a no-candidate message would make the last assertion vacuous.
    const crashed = await crashChat("p10-crash-1", "崩溃前的一条用户消息：以后请用中文回复我");
    // Either the answer was flushed before the abort, or it was not. Both are
    // acceptable; what must hold is the durable state afterwards.
    if (crashed.status !== 200) expect(crashed.status).toBe(0);

    await waitFor(
      async () => (existsSync(markerFile) ? true : undefined),
      30_000,
      "the crash point marker",
    );
    const marker = JSON.parse(readFileSync(markerFile, "utf-8")) as { point: string };
    expect(marker.point).toBe("before_memory_enqueue");

    await killCrashHarness();

    // The crash left the receipt completed AND still pending: nothing was lost,
    // and nothing was half-delivered.
    const state = await receipts.memoryState(CRASH_SESSION_ID, "p10-crash-1");
    expect(state, "the receipt must have completed before the dispatcher picked it up").toBeDefined();
    expect(state?.status).toBe("pending");
    const eventId = await receipts.sourceEventId(CRASH_SESSION_ID, "p10-crash-1");
    expect(await candidateCount(eventId!)).toBe(0);

    // Restart the SAME production entrypoint, no crash point this time. The
    // delivery must happen with no help from any process that was alive before.
    startCrashHarness({ crashPoint: false });
    await waitForCrashHarness();
    await waitFor(
      async () => ((await receipts.memoryState(CRASH_SESSION_ID, "p10-crash-1"))?.status === "done" ? true : undefined),
      30_000,
      "the restarted dispatcher to deliver the pending receipt",
    );
    await killCrashHarness();

    expect(await candidateCount(eventId!)).toBeGreaterThan(0);
  }, 300_000);
});
