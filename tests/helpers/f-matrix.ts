/**
 * Shared machinery for the F1–F14 fault-injection matrix.
 *
 * Two rules the whole matrix obeys:
 *   1. the fault is REAL (a killed process, a destroyed socket, a real MySQL
 *      row), never a stub standing in for the failure;
 *   2. the verdict is read from an AUTHORITY (the business database, the
 *      execution ledger, the receipt log) rather than from the harness's own
 *      account of what it did.
 */

import { spawn, type ChildProcess } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { PI_HARNESS_ROOT, resolveMysqlConfig } from "../../src/config/env.js";
import { mintServiceToken } from "../../src/business/python-client.js";

export const MATRIX_HARNESS_FIXTURE = join(PI_HARNESS_ROOT, "tests", "fixtures", "matrix-harness.ts");

// --- business-side authority reads (the SQLite demo database) ---------------

function withOrderDb<T>(orderDbPath: string, read: (db: DatabaseSync) => T): T {
  const db = new DatabaseSync(orderDbPath);
  try {
    return read(db);
  } finally {
    db.close();
  }
}

export function refundCount(orderDbPath: string): number {
  return withOrderDb(orderDbPath, (db) => {
    const row = db.prepare("SELECT COUNT(*) AS n FROM refunds").get() as { n: number };
    return Number(row.n);
  });
}

export function ticketCount(orderDbPath: string): number {
  return withOrderDb(orderDbPath, (db) => {
    const row = db.prepare("SELECT COUNT(*) AS n FROM support_tickets").get() as { n: number };
    return Number(row.n);
  });
}

export interface LedgerRow {
  idempotency_key: string;
  tool_name: string;
  status: string;
  result_json: string | null;
}

export function ledgerRow(orderDbPath: string, operationId: string): LedgerRow | undefined {
  return withOrderDb(orderDbPath, (db) => {
    const row = db
      .prepare("SELECT idempotency_key, tool_name, status, result_json FROM tool_executions WHERE idempotency_key = ?")
      .get(operationId) as LedgerRow | undefined;
    return row ? { ...row, status: String(row.status) } : undefined;
  });
}

export interface RefundRow {
  refund_id: string;
  status: string;
  requested_at: string;
}

export function refundRowFor(orderDbPath: string, orderId: string): RefundRow | undefined {
  return withOrderDb(orderDbPath, (db) => {
    const row = db
      .prepare("SELECT refund_id, status, requested_at FROM refunds WHERE order_id = ? ORDER BY requested_at DESC LIMIT 1")
      .get(orderId) as RefundRow | undefined;
    return row ? { ...row, status: String(row.status) } : undefined;
  });
}

export function ledgerCount(orderDbPath: string): number {
  return withOrderDb(orderDbPath, (db) => {
    const row = db.prepare("SELECT COUNT(*) AS n FROM tool_executions").get() as { n: number };
    return Number(row.n);
  });
}

/**
 * Leave a claim dangling in the ledger: the execution was claimed and never
 * finished. This is the ONLY way to produce the `UNKNOWN` verdict honestly —
 * it is what a crash between claim and completion really leaves behind.
 */
export function leaveDanglingClaim(orderDbPath: string, operationId: string, tool = "refund_confirm"): void {
  withOrderDb(orderDbPath, (db) => {
    const now = new Date().toISOString();
    db.prepare(
      `INSERT INTO tool_executions
         (idempotency_key, tool_name, arguments_hash, status, attempts, created_at, updated_at)
       VALUES (?, ?, 'dangling', 'in_progress', 1, ?, ?)
       ON CONFLICT(idempotency_key) DO UPDATE SET status = 'in_progress'`,
    ).run(operationId, tool, now, now);
  });
}

// --- internal API calls (harness -> runtime), signed as the harness ---------

export interface InternalCallContext {
  pythonUrl: string;
  accountId: number;
  businessUserId: string;
  sessionId: string;
  clientRequestId: string;
}

export async function internalPost(
  context: InternalCallContext,
  path: string,
  payload: Record<string, unknown>,
): Promise<{ status: number; body: any }> {
  const token = mintServiceToken({
    accountId: context.accountId,
    businessUserId: context.businessUserId,
    sessionId: context.sessionId,
    clientRequestId: context.clientRequestId,
  });
  const response = await fetch(`${context.pythonUrl}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify(payload),
  });
  const text = await response.text();
  let body: unknown = null;
  try {
    body = JSON.parse(text);
  } catch {
    body = text;
  }
  return { status: response.status, body };
}

export async function callToolThroughRuntime(
  context: InternalCallContext,
  tool: string,
  args: Record<string, unknown>,
): Promise<{ status: number; body: any }> {
  return internalPost(context, "/internal/tools/execute", {
    tool,
    arguments: args,
    session_id: context.sessionId,
    client_request_id: context.clientRequestId,
  });
}

/**
 * Open the two-phase flow for real: drive `refund_evaluate` through the live
 * channel so MySQL holds a genuine `pending_action` for the confirm turn.
 */
export async function createPendingRefund(
  context: InternalCallContext,
  orderId: string,
): Promise<string> {
  const result = await callToolThroughRuntime(context, "refund_evaluate", { order_id: orderId });
  const pendingId = result.body?.details?.pending_action_id;
  if (typeof pendingId !== "string" || !pendingId) {
    throw new Error(`refund_evaluate did not open a pending action: ${JSON.stringify(result)}`);
  }
  return pendingId;
}

export async function operationStatus(
  context: InternalCallContext,
  operationId: string,
): Promise<{ status: string; detail: string; result: unknown }> {
  const result = await internalPost(context, "/internal/operation_status", {
    session_id: context.sessionId,
    client_request_id: context.clientRequestId,
    operation_id: operationId,
  });
  if (result.status !== 200) throw new Error(`operation_status failed: ${JSON.stringify(result)}`);
  return { status: String(result.body.status), detail: String(result.body.detail ?? ""), result: result.body.result };
}

// --- real harness process ---------------------------------------------------

export interface MatrixHarness {
  child: ChildProcess;
  port: number;
  url: string;
  stderr: () => string;
  /** Hard kill (no graceful shutdown); resolves when the process is gone. */
  kill: () => Promise<void>;
}

export interface MatrixHarnessOptions {
  port: number;
  pythonUrl: string;
  database: string;
  runtimeCwd: string;
  sessionDir: string;
  agentDir: string;
  scriptFile: string;
  writeMode?: "off" | "shadow" | "live";
  crashPoint?: string;
  crashMarker?: string;
  scriptRepeat?: number;
  /** Shrink the Faux model's context window (F10 forces compaction this way). */
  contextWindow?: number;
  /** Compaction profile overrides (F10 only). */
  compactionReserveTokens?: number;
  compactionKeepRecentTokens?: number;
  /** Test-only control port (F10: on-demand real compaction). */
  controlPort?: number;
}

export async function startMatrixHarness(options: MatrixHarnessOptions): Promise<MatrixHarness> {
  const tsxCli = join(PI_HARNESS_ROOT, "node_modules", "tsx", "dist", "cli.mjs");
  // The child starts from the raw environment, which does NOT carry the shared
  // python-impl/.env secrets; resolve them here and hand them over explicitly.
  const mysql = { ...resolveMysqlConfig(), database: options.database };
  const child = spawn(process.execPath, [tsxCli, MATRIX_HARNESS_FIXTURE], {
    cwd: PI_HARNESS_ROOT,
    env: {
      ...process.env,
      MYSQL_HOST: mysql.host,
      MYSQL_PORT: String(mysql.port),
      MYSQL_USER: mysql.user,
      MYSQL_PASSWORD: mysql.password,
      MATRIX_HARNESS_PORT: String(options.port),
      MATRIX_SCRIPT_FILE: options.scriptFile,
      MATRIX_SCRIPT_REPEAT: String(options.scriptRepeat ?? 4),
      ...(options.contextWindow ? { MATRIX_FAUX_CONTEXT_WINDOW: String(options.contextWindow) } : {}),
      ...(options.compactionReserveTokens
        ? { MATRIX_COMPACTION_RESERVE: String(options.compactionReserveTokens) }
        : {}),
      ...(options.compactionKeepRecentTokens
        ? { MATRIX_COMPACTION_KEEP: String(options.compactionKeepRecentTokens) }
        : {}),
      ...(options.controlPort ? { MATRIX_CONTROL_PORT: String(options.controlPort) } : {}),
      MATRIX_RUNTIME_CWD: options.runtimeCwd,
      MATRIX_SESSION_DIR: options.sessionDir,
      MATRIX_AGENT_DIR: options.agentDir,
      MYSQL_DATABASE: options.database,
      PYTHON_INTERNAL_BASE_URL: options.pythonUrl,
      SMARTCS_WRITE_MODE: options.writeMode ?? "live",
      ...(options.crashPoint ? { SMARTCS_CRASH_POINT: options.crashPoint } : {}),
      ...(options.crashMarker ? { SMARTCS_CRASH_MARKER: options.crashMarker } : {}),
    },
    stdio: ["ignore", "pipe", "pipe"],
  });

  let stderrText = "";
  child.stderr?.on("data", (chunk) => {
    stderrText += String(chunk);
  });

  const url = `http://127.0.0.1:${options.port}`;
  const deadline = Date.now() + 60_000;
  for (;;) {
    if (child.exitCode !== null || child.signalCode !== null) {
      throw new Error(`matrix harness exited during startup (code=${child.exitCode}): ${stderrText}`);
    }
    try {
      const probe = await fetch(`${url}/health`, { signal: AbortSignal.timeout(1_000) });
      if (probe.ok) break;
    } catch {
      /* not up yet */
    }
    if (Date.now() > deadline) throw new Error(`matrix harness did not start: ${stderrText}`);
    await new Promise((r) => setTimeout(r, 200));
  }

  return {
    child,
    port: options.port,
    url,
    stderr: () => stderrText,
    kill: () => hardKill(child),
  };
}

/** SIGKILL-equivalent with no graceful shutdown path. */
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
      child.kill("SIGKILL");
    }
    setTimeout(resolve, 10_000).unref?.();
  });
}

export function crashMarker(path: string): { point: string; at: number; pid: number } | undefined {
  if (!existsSync(path)) return undefined;
  try {
    return JSON.parse(readFileSync(path, "utf-8")) as { point: string; at: number; pid: number };
  } catch {
    return undefined;
  }
}

// --- misc -------------------------------------------------------------------

export async function waitFor(
  predicate: () => boolean | Promise<boolean>,
  options: { timeoutMs?: number; intervalMs?: number; label?: string } = {},
): Promise<void> {
  const timeoutMs = options.timeoutMs ?? 30_000;
  const intervalMs = options.intervalMs ?? 100;
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (await predicate()) return;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${options.label ?? "condition"}`);
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

export function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
