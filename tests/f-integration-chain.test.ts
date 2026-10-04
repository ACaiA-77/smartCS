/**
 * The integrated chain (5d §F6.1 gap).
 *
 * 5d wired the live write branch and proved it with stubs; 5c proved the Python
 * ledger authority with real MySQL. Neither ran the two together. This file
 * does: ONE real Python subprocess, ONE real harness process on real Pi session
 * files, and real injections — the F4 socket drop, the F5 crash window, the F9
 * SSE disconnect — driven through the same live write path, in sequence, with
 * no component swapped for a stub at any point.
 *
 * The three scenarios deliberately run one after another in a single case: the
 * claim under test is that the CHAIN holds end to end, not that each link does
 * in isolation (the isolated evidence lives in the other F files).
 */

import { existsSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { request as httpRequest } from "node:http";
import { join } from "node:path";
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
import { startToolProxy, type ToolProxy } from "./helpers/tool-proxy.js";
import {
  createPendingRefund,
  ledgerRow,
  operationStatus,
  refundCount,
  sleep,
  startMatrixHarness,
  waitFor,
  type MatrixHarness,
} from "./helpers/f-matrix.js";

process.env.INTERNAL_SERVICE_JWT_SECRET =
  process.env.INTERNAL_SERVICE_JWT_SECRET ?? "phase5f-matrix-service-secret-0123456789";

const PY_PORT = 9_140;
const PROXY_PORT = 9_141;
const HARNESS_PORT = 9_142;

type ScriptStep =
  | { kind: "text"; text: string }
  | { kind: "tool"; calls: Array<{ name: string; arguments: Record<string, unknown> }>; text?: string | null }
  | {
      kind: "confirm_router";
      pending_action_id: string;
      confirm_token: string;
      text: string;
      else_text: string;
      repeat: number;
    };

let python: PythonService;
let proxy: ToolProxy;
let db: Database;
let root: string;
let paths: SmartCsPaths;
let orderDbPath: string;
let harness: MatrixHarness | undefined;

// Proxy switches, flipped by the chain as it moves between scenarios.
let dropWriteResponse = false;
let delayWriteForward = false;

function userToken(accountId: number): string {
  const now = Math.floor(Date.now() / 1000);
  return signHs256(
    { sub: String(accountId), iat: now, exp: now + 1800, iss: "smartcs", jti: `jti-${now}-${Math.random()}` },
    userJwtSecret(),
  );
}

function confirmScript(pendingId: string, elseText = "已了解。"): ScriptStep[] {
  return [
    {
      kind: "confirm_router",
      pending_action_id: pendingId,
      confirm_token: "确认退款",
      text: "好的，已处理。",
      else_text: elseText,
      repeat: 8,
    },
  ];
}

async function bootChain(name: string, script: ScriptStep[], crashPoint?: string): Promise<string> {
  await stopChainHarness();
  const scriptFile = join(root, `chain-${name}.json`);
  writeFileSync(scriptFile, JSON.stringify(script), "utf-8");
  const marker = join(root, `chain-${name}.marker`);
  rmSync(marker, { force: true });
  harness = await startMatrixHarness({
    port: HARNESS_PORT,
    pythonUrl: proxy.url,
    database: TEST_DATABASE,
    runtimeCwd: paths.runtimeCwd,
    sessionDir: paths.sessionDir,
    agentDir: paths.agentDir,
    scriptFile,
    writeMode: "live",
    crashPoint,
    crashMarker: crashPoint ? marker : undefined,
  });
  return marker;
}

async function stopChainHarness(): Promise<void> {
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
    signal: AbortSignal.timeout(60_000),
  });
  return { status: response.status, body: (await response.json()) as Record<string, unknown> };
}

function streamThenDisconnect(
  sessionId: string,
  clientRequestId: string,
  message: string,
  account: SeededAccount,
  disconnectAfterMs: number,
): Promise<void> {
  return new Promise((resolve) => {
    const body = JSON.stringify({ session_id: sessionId, client_request_id: clientRequestId, message });
    const req = httpRequest({
      host: "127.0.0.1",
      port: HARNESS_PORT,
      path: "/api/chat/stream",
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Content-Length": Buffer.byteLength(body),
        Authorization: `Bearer ${userToken(account.accountId)}`,
      },
    });
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      resolve();
    };
    req.on("response", (res) => {
      res.on("data", () => {});
      res.on("error", finish);
      res.on("end", finish);
    });
    req.on("error", finish);
    req.write(body);
    req.end();
    setTimeout(() => {
      req.destroy();
      finish();
    }, disconnectAfterMs);
  });
}

function transcriptToolResults(sessionId: string): string {
  const found = SessionManager.findById(paths.runtimeCwd, sessionId, paths.sessionDir);
  if (!found || !existsSync(found)) return "";
  const manager = SessionManager.open(found, paths.sessionDir, paths.runtimeCwd);
  const out: string[] = [];
  for (const entry of manager.getEntries() as Array<{ message?: { role?: string; content?: unknown } }>) {
    if (entry.message?.role !== "toolResult") continue;
    const content = entry.message.content;
    out.push(typeof content === "string" ? content : JSON.stringify(content));
  }
  return out.join("\n");
}

function transcriptEntryCount(sessionId: string): number {
  const found = SessionManager.findById(paths.runtimeCwd, sessionId, paths.sessionDir);
  if (!found || !existsSync(found)) return 0;
  return SessionManager.open(found, paths.sessionDir, paths.runtimeCwd).getEntries().length;
}

async function receiptStatus(sessionId: string, clientRequestId: string): Promise<string | undefined> {
  const rows = await db.query<import("mysql2/promise").RowDataPacket>(
    "SELECT status FROM agent_run_receipt WHERE session_id = ? AND client_request_id = ?",
    [sessionId, clientRequestId],
  );
  return rows.length ? String(rows[0]!.status) : undefined;
}

async function operationIdOf(sessionId: string, clientRequestId: string): Promise<string> {
  const rows = await db.query<import("mysql2/promise").RowDataPacket>(
    "SELECT open_write_operations FROM agent_run_receipt WHERE session_id = ? AND client_request_id = ?",
    [sessionId, clientRequestId],
  );
  const raw = rows[0]!.open_write_operations;
  const ops = (typeof raw === "string" ? JSON.parse(raw) : raw) as Array<{ operation_id: string }>;
  return ops[0]!.operation_id;
}

async function seedOwner(username: string, businessUserId: string, sessionId: string): Promise<SeededAccount> {
  const account = await seedAccount(db, { username, businessUserId });
  await seedSession(db, { sessionId, accountId: account.accountId, harnessVersion: "pi" });
  return account;
}

describe("F matrix — integrated chain (F4 + F5 + F9 on one pipeline)", () => {
  beforeAll(async () => {
    await resetTestDatabase();
    db = testDatabase();
    root = makeTmpDir("phase5f-chain-");
    paths = resolveSmartCsPaths({
      runtimeCwd: join(root, "cwd"),
      sessionDir: join(root, "sessions"),
      agentDir: join(root, "agent"),
    });
    mkdirSync(paths.runtimeCwd, { recursive: true });
    mkdirSync(paths.sessionDir, { recursive: true });
    orderDbPath = join(root, "orders.db");

    // Real Python runtime, live writes enabled.
    python = await startPythonService({ port: PY_PORT, orderDbPath, writeMode: "live" });

    // Real transport in between, so a fault can be a genuine socket event.
    proxy = await startToolProxy({
      port: PROXY_PORT,
      target: python.url,
      rules: [
        {
          matches: ({ url, body }) =>
            delayWriteForward && url.includes("/internal/tools/execute") && body.includes('"refund_confirm"'),
          delayBeforeForwardMs: 1_200,
        },
        {
          matches: ({ url, body }) =>
            dropWriteResponse && url.includes("/internal/tools/execute") && body.includes('"refund_confirm"'),
          dropResponseAfterForward: true,
        },
      ],
    });
  }, 180_000);

  afterAll(async () => {
    await stopChainHarness();
    await proxy?.stop();
    await python?.stop();
    await db?.close();
    try {
      rmSync(root, { recursive: true, force: true });
    } catch {
      /* disposable */
    }
  });

  it("settles F4, F5 and F9 through the same live pipeline without a duplicate write", async () => {
    // --- chain link 1: F4 ---------------------------------------------------
    const f4Session = "chain-f4";
    const f4Account = await seedOwner("chain-f4", "user_006", f4Session);
    const f4Order = "ORD-20260801-0006";
    const beforeF4 = refundCount(orderDbPath);
    const f4Pending = await createPendingRefund(
      { pythonUrl: python.url, accountId: f4Account.accountId, businessUserId: "user_006", sessionId: f4Session, clientRequestId: "chain-f4-eval" },
      f4Order,
    );
    await bootChain("f4", confirmScript(f4Pending));
    dropWriteResponse = true;

    const f4 = await chat(f4Session, "chain-f4-req", `确认退款 ${f4Order}`, f4Account);
    expect(f4.status).toBe(200);
    dropWriteResponse = false;

    expect(transcriptToolResults(f4Session)).toContain("业务已完成");
    expect(refundCount(orderDbPath)).toBe(beforeF4 + 1);
    const f4Operation = await operationIdOf(f4Session, "chain-f4-req");
    expect(ledgerRow(orderDbPath, f4Operation)?.status).toBe("completed");

    // --- chain link 2: F5 ---------------------------------------------------
    const f5Session = "chain-f5";
    const f5Account = await seedOwner("chain-f5", "user_011", f5Session);
    const f5Order = "ORD-20260801-0011";
    const beforeF5 = refundCount(orderDbPath);
    const f5Pending = await createPendingRefund(
      { pythonUrl: python.url, accountId: f5Account.accountId, businessUserId: "user_011", sessionId: f5Session, clientRequestId: "chain-f5-eval" },
      f5Order,
    );

    const marker = await bootChain("f5", confirmScript(f5Pending), "after_write_success");
    await expect(chat(f5Session, "chain-f5-req", `确认退款 ${f5Order}`, f5Account)).rejects.toThrow();
    await waitFor(() => existsSync(marker), { label: "chain F5 crash marker" });

    // The side effect is durable; the transcript never saw the result.
    expect(refundCount(orderDbPath)).toBe(beforeF5 + 1);
    expect(transcriptToolResults(f5Session)).not.toContain("业务已完成");
    const f5Operation = await operationIdOf(f5Session, "chain-f5-req");
    expect(ledgerRow(orderDbPath, f5Operation)?.status).toBe("completed");

    await bootChain("f5b", confirmScript(f5Pending));
    const entriesBeforeRecovery = transcriptEntryCount(f5Session);
    const f5Recovered = await chat(f5Session, "chain-f5-req", `确认退款 ${f5Order}`, f5Account);
    expect(f5Recovered.status).toBe(200);
    expect(String((f5Recovered.body.message as { content: string }).content)).toContain("业务已完成");
    expect(refundCount(orderDbPath)).toBe(beforeF5 + 1);
    // Recovery consulted the ledger, not the model: the transcript did not grow.
    expect(transcriptEntryCount(f5Session)).toBe(entriesBeforeRecovery);
    expect(await receiptStatus(f5Session, "chain-f5-req")).toBe("completed");

    // --- chain link 3: F9 ---------------------------------------------------
    const f9Session = "chain-f9";
    const f9Account = await seedOwner("chain-f9", "user_013", f9Session);
    const f9Order = "ORD-20260801-0013";
    const beforeF9 = refundCount(orderDbPath);
    const f9Pending = await createPendingRefund(
      { pythonUrl: python.url, accountId: f9Account.accountId, businessUserId: "user_013", sessionId: f9Session, clientRequestId: "chain-f9-eval" },
      f9Order,
    );
    await bootChain("f9", confirmScript(f9Pending));
    delayWriteForward = true;

    await streamThenDisconnect(f9Session, "chain-f9-req", `确认退款 ${f9Order}`, f9Account, 600);
    delayWriteForward = false;

    await waitFor(async () => (await receiptStatus(f9Session, "chain-f9-req")) === "completed", {
      label: "chain F9 receipt completion",
      timeoutMs: 30_000,
    });
    expect(refundCount(orderDbPath)).toBe(beforeF9 + 1);

    const f9Operation = await operationIdOf(f9Session, "chain-f9-req");
    const verdict = await operationStatus(
      { pythonUrl: python.url, accountId: f9Account.accountId, businessUserId: "user_013", sessionId: f9Session, clientRequestId: "chain-f9-req" },
      f9Operation,
    );
    expect(verdict.status).toBe("COMPLETED");

    const f9Replay = await chat(f9Session, "chain-f9-req", `确认退款 ${f9Order}`, f9Account);
    expect(f9Replay.body.replayed).toBe(true);
    expect(refundCount(orderDbPath)).toBe(beforeF9 + 1);

    // The chain's cross-cutting claim: three REAL faults on one pipeline, three
    // writes, and never a fourth.
    expect(refundCount(orderDbPath)).toBe(beforeF4 + 3);
  }, 300_000);
});
