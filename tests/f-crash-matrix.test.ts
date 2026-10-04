/**
 * F1 / F3 / F5 / F6 / F12 — crash-window cases.
 *
 * Injection method: a REAL harness child process (`tests/fixtures/matrix-harness.ts`)
 * armed with `SMARTCS_CRASH_POINT`. When the window is reached the process
 * writes a durable marker and aborts — no exit handler, no flush, no chance to
 * report anything afterwards. That is the only honest way to be at a *specific*
 * instruction; an external `taskkill` can only approximate a window.
 *
 * Observation point: the marker file (was the window really reached?), the
 * business SQLite database (did a side effect happen?), the execution ledger
 * (what does the system of record say?), the receipt log in MySQL (what does
 * the harness's durable state say?), and the Pi transcript on disk (what does
 * the model context contain?).
 *
 * Verdict: read from those authorities, never from what the crashed process
 * would have claimed.
 */

import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { RowDataPacket } from "mysql2/promise";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { SessionManager } from "@earendil-works/pi-coding-agent";
import { signHs256 } from "../src/business/jwt-hs256.js";
import { resolveSmartCsPaths, userJwtSecret, type SmartCsPaths } from "../src/config/env.js";
import type { Database } from "../src/db/mysql.js";
import { makeTmpDir } from "./helpers/harness.js";
import {
  resetTestDatabase,
  seedAccount,
  seedSession,
  startPythonService,
  testDatabase,
  TEST_DATABASE,
  type PythonService,
  type SeededAccount,
} from "./helpers/phase1.js";
import {
  crashMarker,
  createPendingRefund,
  ledgerCount,
  ledgerRow,
  refundCount,
  sleep,
  startMatrixHarness,
  ticketCount,
  waitFor,
  type MatrixHarness,
} from "./helpers/f-matrix.js";

process.env.INTERNAL_SERVICE_JWT_SECRET =
  process.env.INTERNAL_SERVICE_JWT_SECRET ?? "phase5f-matrix-service-secret-0123456789";

const PY_PORT = 9_110;
const HARNESS_PORT = 9_111;

type ScriptStep =
  | { kind: "text"; text: string }
  | { kind: "tool"; calls: Array<{ name: string; arguments: Record<string, unknown> }>; text?: string };

let python: PythonService;
let db: Database;
let root: string;
let paths: SmartCsPaths;
let orderDbPath: string;
let harness: MatrixHarness | undefined;

function userToken(accountId: number): string {
  const now = Math.floor(Date.now() / 1000);
  return signHs256(
    { sub: String(accountId), iat: now, exp: now + 1800, iss: "smartcs", jti: `jti-${now}-${Math.random()}` },
    userJwtSecret(),
  );
}

async function bootHarness(
  name: string,
  script: ScriptStep[],
  options: { crashPoint?: string; port?: number } = {},
): Promise<{ harness: MatrixHarness; marker: string }> {
  // Always stop first: binding the same port while a previous child still
  // answers /health would silently hand the test a stale process with a stale
  // Faux script, and every later assertion would be about the wrong run.
  await stopHarness();
  const scriptFile = join(root, `script-${name}.json`);
  writeFileSync(scriptFile, JSON.stringify(script), "utf-8");
  const marker = join(root, `crash-${name}.marker`);
  rmSync(marker, { force: true });
  const started = await startMatrixHarness({
    port: options.port ?? HARNESS_PORT,
    pythonUrl: python.url,
    database: TEST_DATABASE,
    runtimeCwd: paths.runtimeCwd,
    sessionDir: paths.sessionDir,
    agentDir: paths.agentDir,
    scriptFile,
    writeMode: "live",
    crashPoint: options.crashPoint,
    crashMarker: marker,
  });
  return { harness: started, marker };
}

async function stopHarness(): Promise<void> {
  if (!harness) return;
  await harness.kill();
  harness = undefined;
  await sleep(400); // let the listening port be released before rebinding
}

async function chat(
  sessionId: string,
  clientRequestId: string,
  message: string,
  account: SeededAccount,
): Promise<{ status: number; body: Record<string, unknown>; dropped: boolean }> {
  try {
    const response = await fetch(`http://127.0.0.1:${HARNESS_PORT}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${userToken(account.accountId)}` },
      body: JSON.stringify({ session_id: sessionId, client_request_id: clientRequestId, message }),
      signal: AbortSignal.timeout(30_000),
    });
    return { status: response.status, body: (await response.json()) as Record<string, unknown>, dropped: false };
  } catch {
    // A crash mid-request looks like a dropped connection from out here.
    return { status: 0, body: {}, dropped: true };
  }
}

/** mysql2 hands back JSON columns already parsed; tolerate both shapes. */
function asArray(value: unknown): unknown[] {
  if (Array.isArray(value)) return value;
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value);
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  }
  return [];
}

async function receiptOf(sessionId: string, clientRequestId: string): Promise<Record<string, unknown> | undefined> {
  const rows = await db.query<RowDataPacket>(
    "SELECT status, open_write_operations FROM agent_run_receipt WHERE session_id = ? AND client_request_id = ?",
    [sessionId, clientRequestId],
  );
  return rows.length ? (rows[0] as unknown as Record<string, unknown>) : undefined;
}

async function openWritesOf(sessionId: string, clientRequestId: string): Promise<Array<{ operation_id: string }>> {
  const receipt = await receiptOf(sessionId, clientRequestId);
  return asArray(receipt?.open_write_operations) as Array<{ operation_id: string }>;
}

function transcript(sessionId: string): string {
  const found = SessionManager.findById(paths.runtimeCwd, sessionId, paths.sessionDir);
  if (!found || !existsSync(found)) return "";
  const manager = SessionManager.open(found, paths.sessionDir, paths.runtimeCwd);
  return JSON.stringify(manager.getEntries());
}

interface TranscriptEntry {
  type: string;
  message?: { role?: string; content?: unknown };
}

/** The turn's tool activity, read structurally rather than by string match. */
function transcriptShape(sessionId: string): {
  entries: TranscriptEntry[];
  toolCalls: string[];
  toolResults: number;
} {
  const entries = JSON.parse(transcript(sessionId) || "[]") as TranscriptEntry[];
  const toolCalls: string[] = [];
  let toolResults = 0;
  for (const entry of entries) {
    const role = entry.message?.role;
    if (role === "toolResult") toolResults += 1;
    if (role === "assistant" && Array.isArray(entry.message?.content)) {
      for (const block of entry.message!.content as Array<{ type?: string; name?: string }>) {
        if (block?.type === "toolCall" && block.name) toolCalls.push(block.name);
      }
    }
  }
  return { entries, toolCalls, toolResults };
}

async function seedOwner(username: string, businessUserId: string, sessionId: string): Promise<SeededAccount> {
  const account = await seedAccount(db, { username, businessUserId });
  await seedSession(db, { sessionId, accountId: account.accountId, harnessVersion: "pi" });
  return account;
}

describe("F matrix — crash windows (F1/F3/F5/F6/F12)", () => {
  beforeAll(async () => {
    await resetTestDatabase();
    db = testDatabase();
    root = makeTmpDir("phase5f-crash-");
    paths = resolveSmartCsPaths({
      runtimeCwd: join(root, "cwd"),
      sessionDir: join(root, "sessions"),
      agentDir: join(root, "agent"),
    });
    mkdirSync(paths.runtimeCwd, { recursive: true });
    mkdirSync(paths.sessionDir, { recursive: true });
    orderDbPath = join(root, "orders.db");

    python = await startPythonService({ port: PY_PORT, orderDbPath, writeMode: "live" });
  }, 180_000);

  afterAll(async () => {
    await stopHarness();
    await python?.stop();
    await db?.close();
    try {
      rmSync(root, { recursive: true, force: true });
    } catch {
      /* disposable */
    }
  });

  // -------------------------------------------------------------------------
  it("F1 — a crash BEFORE the model runs leaves no side effect and the request can be re-sent", async () => {
    const sessionId = "f1-session";
    const account = await seedOwner("f1-owner", "user_002", sessionId);
    const refundsBefore = refundCount(orderDbPath);
    const ticketsBefore = ticketCount(orderDbPath);
    const ledgerBefore = ledgerCount(orderDbPath);

    const boot = await bootHarness("f1a", [{ kind: "text", text: "（这一轮不应产生回答）" }], {
      crashPoint: "before_llm_call",
    });
    harness = boot.harness;

    const crashed = await chat(sessionId, "f1-req", "我的订单到哪了？", account);
    expect(crashed.dropped, JSON.stringify(crashed.body)).toBe(true);

    await waitFor(() => existsSync(boot.marker), { label: "F1 crash marker" });
    await waitFor(() => harness!.child.exitCode !== null || harness!.child.signalCode !== null, {
      label: "F1 process exit",
    });
    expect(crashMarker(boot.marker)?.point).toBe("before_llm_call");

    // No side effect of any kind, and nothing reached the ledger.
    expect(refundCount(orderDbPath)).toBe(refundsBefore);
    expect(ticketCount(orderDbPath)).toBe(ticketsBefore);
    expect(ledgerCount(orderDbPath)).toBe(ledgerBefore);

    // The receipt is the crash residue: `processing`, with no write operations.
    const residue = await receiptOf(sessionId, "f1-req");
    expect(residue?.status).toBe("processing");
    expect(await openWritesOf(sessionId, "f1-req")).toEqual([]);

    await stopHarness();
    harness = (await bootHarness("f1b", [{ kind: "text", text: "您的订单正在运输中。" }])).harness;

    const retried = await chat(sessionId, "f1-req", "我的订单到哪了？", account);
    expect(retried.status).toBe(200);
    expect(retried.body.replayed).toBe(false);
    expect(String((retried.body.message as { content: string }).content)).toContain("运输中");

    const settled = await receiptOf(sessionId, "f1-req");
    expect(settled?.status).toBe("completed");
    expect(refundCount(orderDbPath)).toBe(refundsBefore);
  }, 180_000);

  // -------------------------------------------------------------------------
  it("F3 — a crash BEFORE the write is sent leaves the ledger empty, and the re-send writes exactly once", async () => {
    const sessionId = "f3-session";
    const account = await seedOwner("f3-owner", "user_004", sessionId);
    const orderId = "ORD-20260801-0004";
    const refundsBefore = refundCount(orderDbPath);
    const ledgerBefore = ledgerCount(orderDbPath);

    const pendingId = await createPendingRefund(
      {
        pythonUrl: python.url,
        accountId: account.accountId,
        businessUserId: "user_004",
        sessionId,
        clientRequestId: "f3-eval",
      },
      orderId,
    );

    const script: ScriptStep[] = [
      { kind: "tool", calls: [{ name: "refund_confirm", arguments: { pending_action_id: pendingId } }] },
    ];

    const boot = await bootHarness("f3a", script, { crashPoint: "before_write_send" });
    harness = boot.harness;

    const crashed = await chat(sessionId, "f3-req", `确认退款 ${orderId}`, account);
    expect(crashed.dropped, JSON.stringify(crashed.body)).toBe(true);

    await waitFor(() => existsSync(boot.marker), { label: "F3 crash marker" });
    expect(crashMarker(boot.marker)?.point).toBe("before_write_send");

    // Nothing left the harness: no refund, and no ledger claim at all.
    expect(refundCount(orderDbPath)).toBe(refundsBefore);
    expect(ledgerCount(orderDbPath)).toBe(ledgerBefore);

    const residue = await receiptOf(sessionId, "f3-req");
    expect(residue?.status).toBe("processing");
    expect(await openWritesOf(sessionId, "f3-req")).toEqual([]);

    await stopHarness();
    harness = (await bootHarness("f3b", script)).harness;

    const retried = await chat(sessionId, "f3-req", `确认退款 ${orderId}`, account);
    expect(retried.status).toBe(200);

    // Exactly one refund, exactly one ledger claim: the retry completed the
    // write that never left, and did not double it.
    expect(refundCount(orderDbPath)).toBe(refundsBefore + 1);
    expect(ledgerCount(orderDbPath)).toBe(ledgerBefore + 1);
    const settled = await receiptOf(sessionId, "f3-req");
    expect(settled?.status).toBe("completed");
  }, 180_000);

  // -------------------------------------------------------------------------
  it("F5 — a crash AFTER the write landed leaves the toolResult unappended, and recovery never re-executes", async () => {
    const sessionId = "f5-session";
    const account = await seedOwner("f5-owner", "user_006", sessionId);
    const orderId = "ORD-20260801-0006";
    const refundsBefore = refundCount(orderDbPath);
    const ledgerBefore = ledgerCount(orderDbPath);

    const pendingId = await createPendingRefund(
      {
        pythonUrl: python.url,
        accountId: account.accountId,
        businessUserId: "user_006",
        sessionId,
        clientRequestId: "f5-eval",
      },
      orderId,
    );

    const script: ScriptStep[] = [
      { kind: "tool", calls: [{ name: "refund_confirm", arguments: { pending_action_id: pendingId } }] },
    ];
    const boot = await bootHarness("f5a", script, { crashPoint: "after_write_success" });
    harness = boot.harness;

    const crashed = await chat(sessionId, "f5-req", `确认退款 ${orderId}`, account);
    expect(crashed.dropped, JSON.stringify(crashed.body)).toBe(true);
    await waitFor(() => existsSync(boot.marker), { label: "F5 crash marker" });
    expect(crashMarker(boot.marker)?.point).toBe("after_write_success");

    // The side effect is REAL and durable...
    expect(refundCount(orderDbPath)).toBe(refundsBefore + 1);

    // ...the receipt already carries the operation id (durable BEFORE sending,
    // which is the only reason any of this is recoverable)...
    const residue = await receiptOf(sessionId, "f5-req");
    expect(residue?.status).toBe("processing");
    const openOps = await openWritesOf(sessionId, "f5-req");
    expect(openOps).toHaveLength(1);
    expect(ledgerRow(orderDbPath, openOps[0]!.operation_id)?.status).toBe("completed");

    // ...but the transcript never learned about it: the injection window is real.
    const shape = transcriptShape(sessionId);
    expect(shape.toolCalls).toContain("refund_confirm");
    expect(shape.toolResults).toBe(0);

    const entriesBefore = shape.entries.length;
    await stopHarness();
    harness = (await bootHarness("f5b", script)).harness;

    const recovered = await chat(sessionId, "f5-req", `确认退款 ${orderId}`, account);
    expect(recovered.status).toBe(200);
    const content = String((recovered.body.message as { content: string }).content);
    expect(content).toContain("业务已完成");

    // No second execution, and — the stronger claim — the model was never
    // consulted again, so it had no opportunity to decide otherwise.
    expect(refundCount(orderDbPath)).toBe(refundsBefore + 1);
    expect(ledgerCount(orderDbPath)).toBe(ledgerBefore + 1);
    expect(transcriptShape(sessionId).entries.length).toBe(entriesBefore);
    expect((await receiptOf(sessionId, "f5-req"))?.status).toBe("completed");
  }, 180_000);

  // -------------------------------------------------------------------------
  it("F6 — a crash AFTER the final, BEFORE the receipt completes, does not duplicate the write", async () => {
    const sessionId = "f6-session";
    const account = await seedOwner("f6-owner", "user_011", sessionId);
    const orderId = "ORD-20260801-0011";
    const refundsBefore = refundCount(orderDbPath);
    const ledgerBefore = ledgerCount(orderDbPath);

    const pendingId = await createPendingRefund(
      {
        pythonUrl: python.url,
        accountId: account.accountId,
        businessUserId: "user_011",
        sessionId,
        clientRequestId: "f6-eval",
      },
      orderId,
    );

    const script: ScriptStep[] = [
      { kind: "tool", calls: [{ name: "refund_confirm", arguments: { pending_action_id: pendingId } }] },
    ];
    const boot = await bootHarness("f6a", script, { crashPoint: "before_receipt_complete" });
    harness = boot.harness;

    const crashed = await chat(sessionId, "f6-req", `确认退款 ${orderId}`, account);
    expect(crashed.dropped, JSON.stringify(crashed.body)).toBe(true);
    await waitFor(() => existsSync(boot.marker), { label: "F6 crash marker" });
    expect(crashMarker(boot.marker)?.point).toBe("before_receipt_complete");

    expect(refundCount(orderDbPath)).toBe(refundsBefore + 1);

    // The F6 window differs from F5: the toolResult AND the final answer were
    // already produced — only the receipt never got its terminal state.
    const shape = transcriptShape(sessionId);
    expect(shape.toolCalls).toContain("refund_confirm");
    expect(shape.toolResults).toBe(1);
    expect((await receiptOf(sessionId, "f6-req"))?.status).toBe("processing");

    await stopHarness();
    harness = (await bootHarness("f6b", script)).harness;

    const recovered = await chat(sessionId, "f6-req", `确认退款 ${orderId}`, account);
    expect(recovered.status).toBe(200);
    expect(String((recovered.body.message as { content: string }).content)).toContain("业务已完成");
    expect(refundCount(orderDbPath)).toBe(refundsBefore + 1);
    expect(ledgerCount(orderDbPath)).toBe(ledgerBefore + 1);
    expect((await receiptOf(sessionId, "f6-req"))?.status).toBe("completed");
  }, 180_000);

  // -------------------------------------------------------------------------
  it("F12 — a hard kill preserves the transcript, the receipt log and the business facts on the same volume", async () => {
    const sessionId = "f12-session";
    const account = await seedOwner("f12-owner", "user_099", sessionId);
    const refundsBefore = refundCount(orderDbPath);

    harness = (await bootHarness("f12", [{ kind: "text", text: "崩溃前生成的第一条回答。" }])).harness;
    const first = await chat(sessionId, "f12-req-1", "崩溃前的第一条消息", account);
    expect(first.status).toBe(200);

    const entriesBefore = JSON.parse(transcript(sessionId)).length;
    expect(entriesBefore).toBeGreaterThan(0);

    // SIGKILL-equivalent: nothing gets a chance to flush.
    await stopHarness();
    expect(JSON.parse(transcript(sessionId)).length).toBe(entriesBefore);
    expect(refundCount(orderDbPath)).toBe(refundsBefore);

    harness = (await bootHarness("f12b", [{ kind: "text", text: "重启后的第二条回答。" }])).harness;
    const second = await chat(sessionId, "f12-req-2", "重启后的第二条消息", account);
    expect(second.status).toBe(200);

    const after = transcript(sessionId);
    expect(JSON.parse(after).length).toBeGreaterThan(entriesBefore);
    // Same session id, same file, both turns present.
    const found = SessionManager.findById(paths.runtimeCwd, sessionId, paths.sessionDir)!;
    const manager = SessionManager.open(found, paths.sessionDir, paths.runtimeCwd);
    expect(manager.getSessionId()).toBe(sessionId);
    const context = JSON.stringify(manager.buildSessionContext().messages);
    expect(context).toContain("崩溃前的第一条消息");
    expect(context).toContain("重启后的第二条消息");

    // The business facts survived on the same volume too.
    expect(refundCount(orderDbPath)).toBe(refundsBefore);
  }, 180_000);
});
