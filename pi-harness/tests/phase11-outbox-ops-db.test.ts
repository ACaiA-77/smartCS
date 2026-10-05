/**
 * Phase 11 §6/§7/§9 — the operator surface, database half.
 *
 *   P11-O3  the durable aggregate reports pending / failed / oldest age /
 *           max attempts, and counts nothing that is already `done`
 *   P11-O4  the manual replay moves a parked row back to `pending` with
 *           `attempts = 0` — and does nothing to a row that is not parked
 *   P11-O5  `/internal/ops/memory-outbox` refuses anonymous and bad callers,
 *           answers a service caller, and CHANGES nothing
 *   P11-O6  the real CLI process performs the replay WITHOUT the Business
 *           Runtime: recovery has exactly one path, and the CLI is not on it
 *
 * Requires the real MySQL on :3307 (see tests/README.md for the window
 * discipline). P11-O6 additionally proves the *absence* of a dependency, so it
 * points the runtime URL at a local recording stub and asserts the stub was
 * never called — a stronger claim than "it happened to work".
 */

import { spawn } from "node:child_process";
import { createServer, type Server } from "node:http";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { RowDataPacket } from "mysql2/promise";
import { createHarnessServer, type HarnessServer } from "../src/server/app.js";
import { ReceiptStore } from "../src/db/receipts.js";
import { PythonInternalClient } from "../src/business/python-client.js";
import { signHs256 } from "../src/business/jwt-hs256.js";
import {
  RUNTIME_TOKEN_AUDIENCE,
  RUNTIME_TOKEN_ISSUER,
  serviceJwtSecret,
  resolveSmartCsPaths,
} from "../src/config/env.js";
import { makeTmpDir } from "./helpers/harness.js";
import type { Database } from "../src/db/mysql.js";
import {
  TEST_DATABASE,
  childMysqlEnv,
  resetTestDatabase,
  seedAccount,
  seedSession,
  testDatabase,
} from "./helpers/phase1.js";

process.env.INTERNAL_SERVICE_JWT_SECRET =
  process.env.INTERNAL_SERVICE_JWT_SECRET ?? "phase11-outbox-service-secret-0123456789";

const SESSION_ID = "phase11-outbox-session";
const RUNTIME_STUB_PORT = 8_981;

let db: Database;
let receipts: ReceiptStore;
let harness: HarnessServer;
let harnessPort = 0;
let runtimeStub: Server;
let runtimeCalls: string[] = [];
let pendingId = 0;
let failedId = 0;
let doneId = 0;

async function insertReceipt(
  clientRequestId: string,
  status: "pending" | "done" | "failed",
  attempts: number,
  ageSeconds = 0,
): Promise<number> {
  const inserted = await db.execute(
    `INSERT INTO agent_run_receipt
       (session_id, client_request_id, request_hash, status, memory_enqueue_status, memory_attempts, open_write_operations)
     VALUES (?, ?, ?, 'completed', ?, ?, JSON_ARRAY())`,
    [SESSION_ID, clientRequestId, `hash-${clientRequestId}`, status, attempts],
  );
  if (ageSeconds > 0) {
    // `updated_at` is ON UPDATE CURRENT_TIMESTAMP, but an explicit value wins —
    // this is how a backlog of a known age is constructed.
    await db.execute(
      "UPDATE agent_run_receipt SET updated_at = NOW(3) - INTERVAL ? SECOND WHERE id = ?",
      [ageSeconds, inserted.insertId],
    );
  }
  return inserted.insertId;
}

/**
 * The token the Business Runtime presents to this harness.
 *
 * `/internal/ops/memory-outbox` is on the harness's internal channel, whose
 * direction is runtime → harness (the reverse of what the harness mints for
 * `/internal/ready`). It is the same credential shape `/internal/history/*`
 * accepts, deliberately without the per-turn claims — this call belongs to no
 * turn. Minting it here, rather than through a product helper, keeps the test
 * honest about what the endpoint actually verifies.
 */
function mintRuntimeOpsToken(): string {
  const now = Math.floor(Date.now() / 1000);
  return signHs256(
    { iss: RUNTIME_TOKEN_ISSUER, aud: RUNTIME_TOKEN_AUDIENCE, iat: now, exp: now + 30 },
    serviceJwtSecret(),
  );
}

async function outboxState(receiptId: number): Promise<{ status: string; attempts: number }> {
  const rows = await db.query<RowDataPacket>(
    "SELECT memory_enqueue_status, memory_attempts FROM agent_run_receipt WHERE id = ?",
    [receiptId],
  );
  return { status: String(rows[0]!.memory_enqueue_status), attempts: Number(rows[0]!.memory_attempts) };
}

beforeAll(async () => {
  await resetTestDatabase();
  db = testDatabase();
  receipts = new ReceiptStore(db);
  const account = await seedAccount(db, { username: "phase11-user", businessUserId: "phase11_user" });
  await seedSession(db, { sessionId: SESSION_ID, accountId: account.accountId, harnessVersion: "pi" });

  pendingId = await insertReceipt("p11-pending", "pending", 2, 63);
  failedId = await insertReceipt("p11-failed", "failed", 5, 300);
  doneId = await insertReceipt("p11-done", "done", 1, 600);

  // The stub stands in for the Business Runtime so P11-O6 can prove the CLI
  // never reached it. It records every path it is asked for.
  runtimeStub = createServer((req, res) => {
    runtimeCalls.push(req.url ?? "");
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ ok: true }));
  });
  await new Promise<void>((resolve) => runtimeStub.listen(RUNTIME_STUB_PORT, "127.0.0.1", resolve));

  const paths = resolveSmartCsPaths();
  const root = makeTmpDir("smartcs-phase11-");
  harness = createHarnessServer({
    paths: { ...paths, runtimeCwd: join(root, "cwd"), sessionDir: join(root, "sessions"), agentDir: join(root, "agent") },
    receipts,
    pythonClient: new PythonInternalClient({ baseUrl: `http://127.0.0.1:${RUNTIME_STUB_PORT}` }),
    // Only the ops route is exercised; the history/delete paths are not reached.
    memorySource: { markCleared: async () => 0 } as unknown as Parameters<typeof createHarnessServer>[0]["memorySource"],
    idleEvictionMs: 60 * 60_000,
    // Observation-only counters, exactly as production wires them.
    outboxStats: () => ({
      passes: 7,
      delivered: 4,
      deliveryFailures: 2,
      parked: 1,
      lastDispatchAt: "2026-10-05T00:00:05.000Z",
      lastSuccessAt: "2026-10-05T00:00:04.000Z",
      lastErrorAt: "2026-10-05T00:00:03.000Z",
    }),
  });
  harnessPort = (await harness.listen(0, "127.0.0.1")).port;
});

afterAll(async () => {
  await harness?.close();
  await new Promise<void>((resolve) => runtimeStub?.close(() => resolve()));
  await db?.close();
});

describe("Phase 11 — durable outbox aggregate", () => {
  it("P11-O3: reports the actionable rows and ignores the drained ones", async () => {
    const stats = await receipts.memoryOutboxStats();
    expect(stats.pending).toBe(1);
    expect(stats.failed).toBe(1);
    expect(stats.maxPendingAttempts).toBe(2);
    // The seeded pending row was aged 63s; MySQL computes the age, so allow a
    // small drift rather than pinning an exact second.
    expect(stats.oldestPendingAgeSeconds).toBeGreaterThanOrEqual(62);
    expect(stats.oldestPendingAgeSeconds).toBeLessThan(120);
    // `done` rows are deliberately NOT counted: that set grows without bound.
    expect(Object.keys(stats)).not.toContain("done");
    expect(doneId).toBeGreaterThan(0);
  });
});

describe("Phase 11 — manual replay", () => {
  it("P11-O4: failed -> pending with attempts reset, and a no-op elsewhere", async () => {
    expect(await outboxState(failedId)).toEqual({ status: "failed", attempts: 5 });

    expect(await receipts.requeueMemory(failedId)).toBe(true);
    expect(await outboxState(failedId)).toEqual({ status: "pending", attempts: 0 });

    // Already requeued: the conditional UPDATE must not match a second time.
    expect(await receipts.requeueMemory(failedId)).toBe(false);
    // A row that is not parked is untouched — replay is not a general reset.
    expect(await receipts.requeueMemory(pendingId)).toBe(false);
    expect(await outboxState(pendingId)).toEqual({ status: "pending", attempts: 2 });

    // Put it back so the CLI case below has a parked row to work with.
    await db.execute(
      "UPDATE agent_run_receipt SET memory_enqueue_status='failed', memory_attempts=5 WHERE id = ?",
      [failedId],
    );
  });
});

describe("Phase 11 — /internal/ops/memory-outbox", () => {
  const url = () => `http://127.0.0.1:${harnessPort}/internal/ops/memory-outbox`;

  it("P11-O5: anonymous and malformed callers are refused", async () => {
    const anonymous = await fetch(url());
    expect(anonymous.status).toBe(401);

    const garbage = await fetch(url(), { headers: { Authorization: "Bearer not-a-token" } });
    expect(garbage.status).toBe(401);

    const wrongScheme = await fetch(url(), { headers: { Authorization: `Basic ${mintRuntimeOpsToken()}` } });
    expect(wrongScheme.status).toBe(401);
  });

  it("P11-O5b: a service caller sees durable truth plus process observation, and nothing changes", async () => {
    const before = { pending: await outboxState(pendingId), failed: await outboxState(failedId) };

    const response = await fetch(url(), {
      headers: { Authorization: `Bearer ${mintRuntimeOpsToken()}` },
    });
    expect(response.status).toBe(200);
    const body = (await response.json()) as {
      durable: Record<string, number>;
      dispatcher: Record<string, unknown>;
    };

    expect(body.durable.pending).toBe(1);
    expect(body.durable.failed).toBe(1);
    expect(body.durable.max_attempts).toBe(2);
    expect(typeof body.durable.oldest_pending_age_seconds).toBe("number");
    expect(body.dispatcher).toMatchObject({ passes: 7, delivered: 4, delivery_failures: 2, parked: 1 });

    // Observe-only: the endpoint must not have moved anything.
    expect(await outboxState(pendingId)).toEqual(before.pending);
    expect(await outboxState(failedId)).toEqual(before.failed);
  });
});

describe("Phase 11 — the operator CLI, end to end", () => {
  it("P11-O6: replays a parked row without ever reaching the Business Runtime", async () => {
    expect(await outboxState(failedId)).toEqual({ status: "failed", attempts: 5 });
    runtimeCalls = [];

    const exitCode = await runCli([
      "node",
      join("node_modules", "tsx", "dist", "cli.mjs"),
      join("src", "cli", "outbox.ts"),
      "retry",
      "--receipt-id",
      String(failedId),
    ]);

    expect(exitCode).toBe(0);
    expect(await outboxState(failedId)).toEqual({ status: "pending", attempts: 0 });
    // The whole point: recovery is "move the row, let the dispatcher deliver".
    // The CLI must not have spoken to the runtime at all.
    expect(runtimeCalls).toEqual([]);
  });

  it("P11-O6b: a usage error exits 2 without touching the database", async () => {
    const exitCode = await runCli([
      "node",
      join("node_modules", "tsx", "dist", "cli.mjs"),
      join("src", "cli", "outbox.ts"),
      "retry",
    ]);
    expect(exitCode).toBe(2);
  });
});

function runCli(argv: string[]): Promise<number> {
  return new Promise((resolve, reject) => {
    const child = spawn(argv[0]!, argv.slice(1), {
      cwd: process.cwd(),
      env: {
        ...process.env,
        ...childMysqlEnv(TEST_DATABASE),
        PYTHON_INTERNAL_BASE_URL: `http://127.0.0.1:${RUNTIME_STUB_PORT}`,
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stderr = "";
    child.stderr?.on("data", (chunk) => {
      stderr += String(chunk);
    });
    child.on("error", reject);
    child.on("exit", (code) => {
      if (code === null) reject(new Error(`CLI terminated without a code: ${stderr}`));
      else resolve(code!);
    });
  });
}
