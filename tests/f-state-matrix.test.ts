/**
 * F7 / F8 / F10 / F13 / F14 — state-authority cases.
 *
 * Injection method: real MySQL rows and real runtime state — a duplicated
 * client_request_id, two simultaneous requests on one session, a compacted
 * transcript, an orphaned `processing` receipt, an expired pending action.
 * Nothing here is simulated at the transport layer; the fault is in the STATE.
 *
 * Observation point: the receipt log, the pending_action table, the execution
 * ledger, the business SQLite database, the Pi transcript.
 *
 * Verdict: the durable authority in each case — never the transcript, never the
 * model's account of what happened.
 */

import { existsSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { RowDataPacket } from "mysql2/promise";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { SessionManager } from "@earendil-works/pi-coding-agent";
import { signHs256 } from "../src/business/jwt-hs256.js";
import { hashRequest } from "../src/db/receipts.js";
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
  createPendingRefund,
  leaveDanglingClaim,
  ledgerCount,
  refundCount,
  sleep,
  startMatrixHarness,
  type MatrixHarness,
} from "./helpers/f-matrix.js";

process.env.INTERNAL_SERVICE_JWT_SECRET =
  process.env.INTERNAL_SERVICE_JWT_SECRET ?? "phase5f-matrix-service-secret-0123456789";

const PY_PORT = 9_130;
const HARNESS_PORT = 9_131;
const CONTROL_PORT = 9_132;

type ScriptStep =
  | { kind: "text"; text: string }
  | {
      kind: "tool";
      calls: Array<{ name: string; arguments: Record<string, unknown> }>;
      text?: string | null;
    }
  | {
      /** Scenario F10: decide by the newest user message, not by call order. */
      kind: "confirm_router";
      pending_action_id: string;
      confirm_token: string;
      text: string;
      else_text: string;
      repeat: number;
    };

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
  options: { compactionReserveTokens?: number; compactionKeepRecentTokens?: number } = {},
): Promise<void> {
  await stopHarness();
  const scriptFile = join(root, `script-${name}.json`);
  writeFileSync(scriptFile, JSON.stringify(script), "utf-8");
  harness = await startMatrixHarness({
    port: HARNESS_PORT,
    pythonUrl: python.url,
    database: TEST_DATABASE,
    runtimeCwd: paths.runtimeCwd,
    sessionDir: paths.sessionDir,
    agentDir: paths.agentDir,
    scriptFile,
    writeMode: "live",
    scriptRepeat: 8,
    // Test-only control surface: lets F10 invoke the runtime's real
    // `AgentSession.compact()` instead of hoping a threshold is crossed.
    controlPort: CONTROL_PORT,
    compactionReserveTokens: options.compactionReserveTokens,
    compactionKeepRecentTokens: options.compactionKeepRecentTokens,
  });
}

/** Ask the harness process to run REAL compaction on a live session. */
async function compactSession(sessionId: string): Promise<void> {
  const response = await fetch(`http://127.0.0.1:${CONTROL_PORT}/compact`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
    signal: AbortSignal.timeout(60_000),
  });
  if (!response.ok) throw new Error(`compaction control call failed: ${response.status} ${await response.text()}`);
}

async function stopHarness(): Promise<void> {
  if (!harness) return;
  await harness.kill();
  harness = undefined;
  await sleep(400);
}

async function chat(
  sessionId: string,
  clientRequestId: string,
  message: string,
  account: SeededAccount,
): Promise<{ status: number; body: Record<string, unknown> }> {
  const response = await fetch(`http://127.0.0.1:${HARNESS_PORT}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${userToken(account.accountId)}` },
    body: JSON.stringify({ session_id: sessionId, client_request_id: clientRequestId, message }),
    signal: AbortSignal.timeout(120_000),
  });
  return { status: response.status, body: (await response.json()) as Record<string, unknown> };
}

async function receiptRow(sessionId: string, clientRequestId: string): Promise<Record<string, unknown> | undefined> {
  const rows = await db.query<RowDataPacket>(
    "SELECT id, status, response, open_write_operations FROM agent_run_receipt WHERE session_id = ? AND client_request_id = ?",
    [sessionId, clientRequestId],
  );
  return rows.length ? (rows[0] as unknown as Record<string, unknown>) : undefined;
}

function transcriptEntries(sessionId: string): Array<{ message?: { role?: string; content?: unknown } }> {
  const found = SessionManager.findById(paths.runtimeCwd, sessionId, paths.sessionDir);
  if (!found || !existsSync(found)) return [];
  return SessionManager.open(found, paths.sessionDir, paths.runtimeCwd).getEntries() as Array<{
    message?: { role?: string; content?: unknown };
  }>;
}

function transcriptToolResults(sessionId: string): string[] {
  const out: string[] = [];
  for (const entry of transcriptEntries(sessionId)) {
    if (entry.message?.role !== "toolResult") continue;
    const content = entry.message.content;
    if (typeof content === "string") out.push(content);
    else if (Array.isArray(content)) {
      out.push(content.map((block) => String((block as { text?: string }).text ?? "")).join(""));
    }
  }
  return out;
}

async function seedOwner(username: string, businessUserId: string, sessionId: string): Promise<SeededAccount> {
  const account = await seedAccount(db, { username, businessUserId });
  await seedSession(db, { sessionId, accountId: account.accountId, harnessVersion: "pi" });
  return account;
}

async function pendingStatus(pendingId: string): Promise<string | undefined> {
  const rows = await db.query<RowDataPacket>("SELECT status FROM pending_action WHERE id = ?", [pendingId]);
  return rows.length ? String(rows[0]!.status) : undefined;
}

async function confirmRefund(
  sessionId: string,
  orderId: string,
  businessUserId: string,
  account: SeededAccount,
  evalRequestId: string,
): Promise<string> {
  const pendingId = await createPendingRefund(
    { pythonUrl: python.url, accountId: account.accountId, businessUserId, sessionId, clientRequestId: evalRequestId },
    orderId,
  );
  await bootHarness(`confirm-${sessionId}`, [
    { kind: "tool", calls: [{ name: "refund_confirm", arguments: { pending_action_id: pendingId } }] },
  ]);
  return pendingId;
}

describe("F matrix — state authority (F7/F8/F10/F13/F14)", () => {
  beforeAll(async () => {
    await resetTestDatabase();
    db = testDatabase();
    root = makeTmpDir("phase5f-state-");
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
  it("F7 — the same client_request_id is replayed, never run twice", async () => {
    const sessionId = "f7-session";
    const account = await seedOwner("f7-owner", "user_006", sessionId);
    const orderId = "ORD-20260801-0006";
    const refundsBefore = refundCount(orderDbPath);

    await confirmRefund(sessionId, orderId, "user_006", account, "f7-eval");
    const first = await chat(sessionId, "f7-req", `确认退款 ${orderId}`, account);
    expect(first.status).toBe(200);
    expect(refundCount(orderDbPath)).toBe(refundsBefore + 1);

    const entriesAfterFirst = transcriptEntries(sessionId).length;
    const ledgerAfterFirst = ledgerCount(orderDbPath);

    const replay = await chat(sessionId, "f7-req", `确认退款 ${orderId}`, account);
    expect(replay.status).toBe(200);
    expect(replay.body.replayed).toBe(true);
    expect(String((replay.body.message as { content: string }).content)).toBe(
      String((first.body.message as { content: string }).content),
    );

    // No second turn, no second side effect, no second ledger claim.
    expect(transcriptEntries(sessionId).length).toBe(entriesAfterFirst);
    expect(refundCount(orderDbPath)).toBe(refundsBefore + 1);
    expect(ledgerCount(orderDbPath)).toBe(ledgerAfterFirst);
  }, 180_000);

  // -------------------------------------------------------------------------
  it("F8 — two simultaneous requests on one session are serialised by the single-writer lease", async () => {
    const sessionId = "f8-session";
    const account = await seedOwner("f8-owner", "user_099", sessionId);

    await bootHarness("f8", [{ kind: "text", text: "已收到。" }]);

    const [a, b] = await Promise.all([
      chat(sessionId, "f8-req-a", "第一个并发请求", account),
      chat(sessionId, "f8-req-b", "第二个并发请求", account),
    ]);

    // Exactly one may be turned away as busy; whatever the admission decision,
    // it must not be "both ran at once".
    const statuses = [a.status, b.status].sort();
    expect(statuses.every((status) => status === 200 || status === 429)).toBe(true);
    expect(statuses[0]).toBe(200);
    expect(a.status === 200 || b.status === 200).toBe(true);
    if (statuses[0] === 429) {
      expect(statuses[1]).toBe(429);
    }

    const rows = await db.query<RowDataPacket>(
      "SELECT id, client_request_id, status FROM agent_run_receipt WHERE session_id = ? ORDER BY id ASC",
      [sessionId],
    );
    // Serialisation order is receipt-id order (the lease is taken first), so the
    // transcript's user turns must appear in exactly that order.
    const userTurns = transcriptEntries(sessionId)
      .filter((entry) => entry.message?.role === "user")
      .map((entry) => JSON.stringify(entry.message?.content));
    const expectedOrder = rows.map((row) =>
      String(row.client_request_id) === "f8-req-a" ? "第一个并发请求" : "第二个并发请求",
    );
    expect(userTurns.length).toBeGreaterThanOrEqual(1);
    for (const [index, text] of expectedOrder.entries()) {
      if (index >= userTurns.length) break;
      expect(userTurns[index]).toContain(text);
    }
    // One canonical receipt per admitted request, each completed at most once.
    for (const row of rows) {
      expect(["processing", "completed"]).toContain(String(row.status));
    }
  }, 180_000);

  // -------------------------------------------------------------------------
  it("F10 — a refund confirmed AFTER compaction still resolves from Python state, not from the transcript", async () => {
    const sessionId = "f10-session";
    const account = await seedOwner("f10-owner", "user_003", sessionId);
    const orderId = "ORD-20260801-0003";
    const refundsBefore = refundCount(orderDbPath);

    const pendingId = await createPendingRefund(
      {
        pythonUrl: python.url,
        accountId: account.accountId,
        businessUserId: "user_003",
        sessionId,
        clientRequestId: "f10-eval",
      },
      orderId,
    );

    // The model script decides by the newest user message rather than by call
    // order, because compaction asks the SAME provider for a summary and would
    // otherwise eat the scripted confirm call.
    await bootHarness("f10", [
      {
        kind: "confirm_router",
        pending_action_id: pendingId,
        confirm_token: "确认退款",
        text: "好的，已处理。",
        else_text: "已了解您的订单情况。",
        repeat: 8,
      },
      // A stock profile keeps a 16k recent window, which for this short session
      // means "nothing to summarise". The point of F10 is the confirm AFTER a
      // real compaction, so the cut point is set where one will actually happen.
    ], { compactionReserveTokens: 1_000, compactionKeepRecentTokens: 200 });

    // Several substantial turns: a summary needs history to summarise, and one
    // short turn leaves the cut point with nothing on the far side of it.
    const filler = "订单相关的背景说明。".repeat(200);
    for (let index = 0; index < 4; index += 1) {
      const turn = await chat(sessionId, `f10-filler-${index}`, `第 ${index} 轮背景：我的订单 ${orderId}。${filler}`, account);
      expect(turn.status).toBe(200);
    }

    // Compaction really happens, through the runtime's own `compact()` path.
    const entriesBefore = transcriptEntries(sessionId).length;
    await compactSession(sessionId);
    const afterCompaction = transcriptEntries(sessionId);
    expect(afterCompaction.some((entry) => String((entry as { type?: string }).type).startsWith("compaction"))).toBe(
      true,
    );
    expect(afterCompaction.length).toBeGreaterThan(entriesBefore);

    const confirm = await chat(sessionId, "f10-confirm", `确认退款 ${orderId}`, account);
    expect(confirm.status).toBe(200);

    // And the write still happened, from MySQL state the model never saw.
    const toolEvidence = transcriptToolResults(sessionId).join("\n");
    expect(refundCount(orderDbPath), toolEvidence).toBe(refundsBefore + 1);
    expect(await pendingStatus(pendingId)).toBe("consumed");
    expect((await receiptRow(sessionId, "f10-confirm"))?.status).toBe("completed");
  }, 240_000);

  // -------------------------------------------------------------------------
  it("F13 — an orphaned processing receipt with a COMPLETED write is reconciled, not blindly re-run", async () => {
    const sessionId = "f13-session";
    const account = await seedOwner("f13-owner", "user_004", sessionId);
    const orderId = "ORD-20260801-0004";
    const refundsBefore = refundCount(orderDbPath);

    await confirmRefund(sessionId, orderId, "user_004", account, "f13-eval");
    const done = await chat(sessionId, "f13-req", `确认退款 ${orderId}`, account);
    expect(done.status).toBe(200);
    expect(refundCount(orderDbPath)).toBe(refundsBefore + 1);

    const entriesAfterDone = transcriptEntries(sessionId).length;

    // Simulate the owner dying between "final produced" and "receipt completed"
    // (the F6 residue), then a NEW owner picking the request up.
    await db.execute("UPDATE agent_run_receipt SET status = 'processing' WHERE session_id = ? AND client_request_id = ?", [
      sessionId,
      "f13-req",
    ]);
    expect((await receiptRow(sessionId, "f13-req"))?.status).toBe("processing");

    const recovered = await chat(sessionId, "f13-req", `确认退款 ${orderId}`, account);
    expect(recovered.status).toBe(200);
    expect(String((recovered.body.message as { content: string }).content)).toContain("业务已完成");

    // Not re-run: no new model turn, no new refund, one canonical answer.
    expect(transcriptEntries(sessionId).length).toBe(entriesAfterDone);
    expect(refundCount(orderDbPath)).toBe(refundsBefore + 1);
    expect((await receiptRow(sessionId, "f13-req"))?.status).toBe("completed");

    const replay = await chat(sessionId, "f13-req", `确认退款 ${orderId}`, account);
    expect(replay.body.replayed).toBe(true);
    expect(String((replay.body.message as { content: string }).content)).toBe(
      String((recovered.body.message as { content: string }).content),
    );
  }, 240_000);

  // -------------------------------------------------------------------------
  it("F13b — an orphan whose ledger verdict is UNKNOWN is refused, never re-run", async () => {
    const sessionId = "f13b-session";
    const account = await seedOwner("f13b-owner", "user_098", sessionId);
    const refundsBefore = refundCount(orderDbPath);

    // A claim that was taken and never finished: the honest residue of a crash
    // between claim and completion. No verdict can be derived from it.
    const danglingOperation = "op-dangling-f13b";
    leaveDanglingClaim(orderDbPath, danglingOperation);

    await db.execute(
      `INSERT INTO agent_run_receipt (session_id, client_request_id, request_hash, status, open_write_operations)
       VALUES (?, ?, ?, 'processing', CAST(? AS JSON))`,
      [sessionId, "f13b-req", hashRequest("继续处理我的退款"), JSON.stringify([{ operation_id: danglingOperation, tool: "refund_confirm", state: "PREPARED", target_hash: "h" }])],
    );

    await bootHarness("f13b", [{ kind: "text", text: "这一轮不应被模型执行。" }]);
    const entriesBefore = transcriptEntries(sessionId).length;

    const refused = await chat(sessionId, "f13b-req", "继续处理我的退款", account);
    expect(refused.status).toBe(409);
    expect(String((refused.body.detail as string) ?? "")).toContain("未确定");

    // The refusal is the point: nothing ran, nothing was written, and the
    // residual state is left for a human rather than guessed away.
    expect(transcriptEntries(sessionId).length).toBe(entriesBefore);
    expect(refundCount(orderDbPath)).toBe(refundsBefore);
    expect((await receiptRow(sessionId, "f13b-req"))?.status).toBe("processing");
  }, 180_000);

  // -------------------------------------------------------------------------
  it("F14 — a late confirmation of an expired pending action is refused with zero writes", async () => {
    const sessionId = "f14-session";
    const account = await seedOwner("f14-owner", "user_005", sessionId);
    const orderId = "ORD-20260801-0005";
    const refundsBefore = refundCount(orderDbPath);

    const pendingId = await createPendingRefund(
      {
        pythonUrl: python.url,
        accountId: account.accountId,
        businessUserId: "user_005",
        sessionId,
        clientRequestId: "f14-eval",
      },
      orderId,
    );
    expect(await pendingStatus(pendingId)).toBe("pending");

    // Time passes: the offer expires.
    await db.execute("UPDATE pending_action SET expires_at = DATE_SUB(NOW(3), INTERVAL 1 HOUR) WHERE id = ?", [pendingId]);

    await bootHarness("f14", [
      { kind: "tool", calls: [{ name: "refund_confirm", arguments: { pending_action_id: pendingId } }] },
    ]);
    const late = await chat(sessionId, "f14-req", `确认退款 ${orderId}`, account);
    expect(late.status).toBe(200);

    const results = transcriptToolResults(sessionId).join("\n");
    expect(results).toContain("已过期");

    expect(refundCount(orderDbPath)).toBe(refundsBefore);
    expect(await pendingStatus(pendingId)).toBe("expired");
  }, 180_000);
});
