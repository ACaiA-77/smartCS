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
import { randomBytes } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { PI_HARNESS_ROOT, resolveMysqlConfig } from "../../src/config/env.js";
import { mintServiceToken } from "../../src/business/python-client.js";
import { hardKill, treeSpawnOptions } from "./harness.js";

export const MATRIX_HARNESS_FIXTURE = join(PI_HARNESS_ROOT, "tests", "fixtures", "matrix-harness.ts");

// --- business-side authority reads (the Copilot SQLite database) ----------

function withCopilotDb<T>(copilotDbPath: string, read: (db: DatabaseSync) => T): T {
  const db = new DatabaseSync(copilotDbPath);
  try {
    db.exec("PRAGMA busy_timeout = 5000;");
    return read(db);
  } finally {
    db.close();
  }
}

export function sentMailCount(copilotDbPath: string): number {
  return withCopilotDb(copilotDbPath, (db) => {
    const row = db.prepare("SELECT COUNT(*) AS n FROM test_sent_mails").get() as { n: number };
    return Number(row.n);
  });
}

export interface LedgerRow {
  idempotency_key: string;
  tool_name: string;
  status: string;
  result_json: string | null;
}

export function ledgerRow(copilotDbPath: string, operationId: string): LedgerRow | undefined {
  return withCopilotDb(copilotDbPath, (db) => {
    const row = db
      .prepare("SELECT idempotency_key, tool_name, status, result_json FROM tool_executions WHERE idempotency_key = ?")
      .get(operationId) as LedgerRow | undefined;
    return row ? { ...row, status: String(row.status) } : undefined;
  });
}

export interface SentMailRow {
  message_id: string;
  account: string;
  subject: string;
  sent_at: string;
}

export function sentMailRowFor(copilotDbPath: string, subject: string): SentMailRow | undefined {
  return withCopilotDb(copilotDbPath, (db) => {
    const row = db
      .prepare("SELECT message_id, account, subject, sent_at FROM test_sent_mails WHERE subject = ? ORDER BY sent_at DESC LIMIT 1")
      .get(subject) as SentMailRow | undefined;
    return row ? { ...row, subject: String(row.subject) } : undefined;
  });
}

export function ledgerCount(copilotDbPath: string): number {
  return withCopilotDb(copilotDbPath, (db) => {
    const row = db.prepare("SELECT COUNT(*) AS n FROM tool_executions").get() as { n: number };
    return Number(row.n);
  });
}

/**
 * Leave a claim dangling in the ledger: the execution was claimed and never
 * finished. This is the ONLY way to produce the `UNKNOWN` verdict honestly —
 * it is what a crash between claim and completion really leaves behind.
 */
export function leaveDanglingClaim(copilotDbPath: string, operationId: string, tool = "mail_send"): void {
  withCopilotDb(copilotDbPath, (db) => {
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
 * Open the two-phase flow for real: drive `copilot_action_prepare` through the
 * live channel so MySQL holds a genuine `pending_action` for the confirm turn.
 */
export async function createPendingMailAction(
  context: InternalCallContext,
  options: { account?: string; to?: string; subject: string; body?: string },
): Promise<string> {
  const result = await callToolThroughRuntime(context, "copilot_action_prepare", {
    tool: "mail_send",
    arguments: {
      account: options.account ?? "163",
      to: options.to ?? "alice@example.com",
      subject: options.subject,
      body: options.body ?? `Status update for ${options.subject}`,
    },
  });
  const pendingId = result.body?.details?.pending_action_id;
  if (typeof pendingId !== "string" || !pendingId) {
    throw new Error(`copilot_action_prepare did not open a pending action: ${JSON.stringify(result)}`);
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
  // A per-boot identity. `/health` from ANY other process — a leftover from a
  // previous boot on the same port — must never be accepted as this boot's
  // readiness signal, or every later assertion is about the wrong run.
  const bootToken = randomBytes(8).toString("hex");
  const child = spawn(process.execPath, [tsxCli, MATRIX_HARNESS_FIXTURE], {
    cwd: PI_HARNESS_ROOT,
    ...treeSpawnOptions(),
    env: {
      ...process.env,
      MYSQL_HOST: mysql.host,
      MYSQL_PORT: String(mysql.port),
      MYSQL_USER: mysql.user,
      MYSQL_PASSWORD: mysql.password,
      MATRIX_HARNESS_PORT: String(options.port),
      MATRIX_BOOT_TOKEN: bootToken,
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
  child.stderr?.on("data", (chunk: Buffer) => {
    stderrText += String(chunk);
  });
  // The fixture prints one JSON line once ITS socket is bound. Waiting for the
  // line that carries this boot's token — instead of polling `/health` — is
  // what keeps a survivor from a previous boot from being mistaken for this one.
  let stdoutText = "";
  let listening = false;
  child.stdout?.on("data", (chunk: Buffer) => {
    stdoutText += String(chunk);
    for (const line of stdoutText.split("\n")) {
      if (!line.trim().startsWith("{")) continue;
      try {
        const event = JSON.parse(line) as { event?: string; bootToken?: string };
        if (event.event === "listening" && event.bootToken === bootToken) listening = true;
      } catch {
        /* partial line: wait for more */
      }
    }
  });

  const url = `http://127.0.0.1:${options.port}`;
  const deadline = Date.now() + 60_000;
  for (;;) {
    if (child.exitCode !== null || child.signalCode !== null) {
      throw new Error(`matrix harness exited during startup (code=${child.exitCode}): ${stderrText}`);
    }
    if (listening) {
      const probe = await fetch(`${url}/health`, { signal: AbortSignal.timeout(1_000) }).catch(() => undefined);
      if (probe?.ok) break;
    }
    if (Date.now() > deadline) throw new Error(`matrix harness did not start: ${stderrText}\n${stdoutText}`);
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
