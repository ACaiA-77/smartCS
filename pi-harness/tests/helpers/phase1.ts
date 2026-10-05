/**
 * Phase 1 test infrastructure.
 *
 * Uses the REAL MySQL instance (compose :3307) against an isolated database
 * (`smartcs_phase1_test`), applies the real migration file, and — where the
 * scenario needs it — boots the REAL Python internal_api in a subprocess.
 * Nothing here is a mock of the thing under test.
 */

import { spawn, type ChildProcess } from "node:child_process";
import { createConnection } from "mysql2/promise";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createDatabase, type Database } from "../../src/db/mysql.js";
import { PI_HARNESS_ROOT, PYTHON_IMPL_ROOT, pythonEnv, resolveMysqlConfig } from "../../src/config/env.js";

const HERE = dirname(fileURLToPath(import.meta.url));
/**
 * The Python Business Runtime root. In the monorepo the two runtimes share the
 * Git root, so this is the repository root — see `PYTHON_IMPL_ROOT` in
 * `src/config/env.ts`.
 */
export const PYTHON_IMPL = PYTHON_IMPL_ROOT;
export const MIGRATION_FILES = [
  join(PYTHON_IMPL, "migrations", "001_phase1_session_foundation.sql"),
  join(PYTHON_IMPL, "migrations", "002_phase3_memory_outbox.sql"),
  // Phase 5: pending_action is the two-phase refund authority. Additive and
  // idempotent, so earlier phases' tests are unaffected.
  join(PYTHON_IMPL, "migrations", "003_phase5_write_enable.sql"),
  // Phase 6: the audit sink. Additive and idempotent for the same reason.
  join(PYTHON_IMPL, "migrations", "004_phase6_audit.sql"),
];

export const TEST_DATABASE = process.env.SMARTCS_TEST_DATABASE ?? "smartcs_phase1_test";

export function testMysqlConfig() {
  return { ...resolveMysqlConfig(), database: TEST_DATABASE };
}

/**
 * The MYSQL_* environment a spawned Python service must receive.
 *
 * Resolved with the SAME rule the test process uses (process env first, then
 * `python-impl/.env` via `envValue`) and handed over explicitly, instead of
 * relying on whatever the child happens to inherit. A clean shell without
 * `MYSQL_PASSWORD` exported used to be able to start a runtime that then could
 * not reach its platform database — a configuration error that surfaced much
 * later as a confusing refusal. Now it fails here, with a message naming the
 * missing variable.
 */
export function childMysqlEnv(database: string = TEST_DATABASE): Record<string, string> {
  let mysql;
  try {
    mysql = resolveMysqlConfig({ database });
  } catch (error) {
    throw new Error(
      "test fixtures need MySQL credentials: export MYSQL_PASSWORD or set it in the repository-root .env",
      { cause: error },
    );
  }
  return {
    MYSQL_HOST: mysql.host,
    MYSQL_PORT: String(mysql.port),
    MYSQL_USER: mysql.user,
    MYSQL_PASSWORD: mysql.password,
    MYSQL_DATABASE: mysql.database,
  };
}

/** Drop + recreate the Phase 1 tables and apply the real migration script. */
export async function resetTestDatabase(): Promise<void> {
  const config = testMysqlConfig();
  const connection = await createConnection({ ...config, multipleStatements: true, charset: "utf8mb4" });
  try {
    await connection.query("DROP TABLE IF EXISTS audit_event");
    await connection.query("DROP TABLE IF EXISTS pending_action");
    await connection.query("DROP TABLE IF EXISTS memory_source_event");
    await connection.query("DROP TABLE IF EXISTS agent_run_receipt");
    await connection.query("DROP TABLE IF EXISTS conversation_session");
    await connection.query("DROP TABLE IF EXISTS platform_user");
    // Mirror of PlatformDatabase.initialize (pre-Phase-1 shape).
    await connection.query(`CREATE TABLE platform_user (
      id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
      username VARCHAR(128) COLLATE utf8mb4_bin NOT NULL UNIQUE,
      password_hash VARCHAR(512) NOT NULL,
      business_user_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL UNIQUE,
      status VARCHAR(16) NOT NULL DEFAULT 'active',
      created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
      updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`);
    await connection.query(`CREATE TABLE conversation_session (
      session_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL PRIMARY KEY,
      account_id BIGINT NOT NULL,
      title VARCHAR(200) NOT NULL DEFAULT '',
      client_request_id VARCHAR(128) COLLATE utf8mb4_bin NULL,
      created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
      updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
      UNIQUE KEY account_initial_request (account_id, client_request_id),
      KEY account_recent_session (account_id, updated_at),
      CONSTRAINT session_account_fk FOREIGN KEY (account_id) REFERENCES platform_user(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`);

    // The migrations themselves. PREPARE/EXECUTE are session-scoped, so each
    // script must run on this one connection, in order.
    for (const migration of MIGRATION_FILES) {
      await connection.query(readFileSync(migration, "utf-8"));
    }
  } finally {
    await connection.end();
  }
}

export function testDatabase(): Database {
  return createDatabase(testMysqlConfig());
}

export interface SeededAccount {
  accountId: number;
  username: string;
  businessUserId: string;
}

/**
 * Idempotent on purpose: an aborted earlier run (or a run that was interrupted
 * mid-suite) leaves rows behind, and BOTH unique keys — `username` and
 * `business_user_id` — would then fail THIS run with a duplicate-entry error
 * that has nothing to do with the behaviour under test. Residue is removed
 * first, sessions before accounts because the session row carries the FK.
 */
export async function seedAccount(
  db: Database,
  options: { username: string; businessUserId: string; status?: string },
): Promise<SeededAccount> {
  await db.execute(
    `DELETE FROM conversation_session WHERE account_id IN
       (SELECT id FROM platform_user WHERE username = ? OR business_user_id = ?)`,
    [options.username, options.businessUserId],
  );
  await db.execute("DELETE FROM platform_user WHERE username = ? OR business_user_id = ?", [
    options.username,
    options.businessUserId,
  ]);
  await db.execute(
    `INSERT INTO platform_user (username, password_hash, business_user_id, status)
     VALUES (?, 'not-a-real-hash', ?, ?)`,
    [options.username, options.businessUserId, options.status ?? "active"],
  );
  const rows = await db.query<import("mysql2/promise").RowDataPacket>(
    "SELECT id FROM platform_user WHERE username = ?",
    [options.username],
  );
  return { accountId: Number(rows[0]!.id), username: options.username, businessUserId: options.businessUserId };
}

/** Idempotent for the same reason as `seedAccount`: a stale row is replaced. */
export async function seedSession(
  db: Database,
  options: { sessionId: string; accountId: number; harnessVersion: "legacy" | "pi"; title?: string },
): Promise<void> {
  await db.execute("DELETE FROM conversation_session WHERE session_id = ?", [options.sessionId]);
  await db.execute(
    `INSERT INTO conversation_session (session_id, account_id, title, harness_version)
     VALUES (?, ?, ?, ?)`,
    [options.sessionId, options.accountId, options.title ?? "", options.harnessVersion],
  );
}

export interface PythonService {
  port: number;
  url: string;
  stop: () => Promise<void>;
  stderr: () => string;
}

/**
 * Boot the REAL python-impl/internal_api in a subprocess.
 *
 * A tiny inline ASGI app mounts the production router with real
 * MySQL-backed app.state, so the TS harness talks to genuine Python code over
 * genuine HTTP. Booting the full api.main is avoided on purpose: its lifespan
 * loads RAG models and a memory worker, none of which Phase 1 exercises.
 */
export async function startPythonService(options: {
  port: number;
  database?: string;
  /** When set, the real ToolExecutor is wired over this SQLite demo database. */
  orderDbPath?: string;
  /**
   * Phase 5: `live` turns on the real write channel. It must match the harness
   * side (`SMARTCS_WRITE_MODE`); the runtime refuses write tools unless it is
   * explicitly live, which is what keeps a shadow deployment safe.
   */
  writeMode?: "off" | "shadow" | "live";
  /**
   * Phase 6: expose the in-process trace records the internal router collects.
   * Off by default (the endpoint answers 404), exactly like production.
   */
  traceRecords?: boolean;
}): Promise<PythonService> {
  const env = pythonEnv();
  const program = `
import os, uvicorn
from fastapi import FastAPI
from internal_api import internal_router
from platform_db.database import PlatformDatabase
from platform_db.users import Users
from platform_db.sessions import Sessions

app = FastAPI()
app.include_router(internal_router)

@app.on_event("startup")
async def _startup():
    db = PlatformDatabase.from_env()
    await db.initialize()
    app.state.platform_users = Users(db)
    app.state.platform_sessions = Sessions(db)
    # Phase 5: the authorization service reads the durable user message and
    # writes pending_action / open_write_operations through the raw transaction.
    app.state.platform_database = db
    order_db = os.environ.get("SMARTCS_TEST_ORDER_DB")
    if order_db:
        from mcp.execution_ledger import ExecutionLedger
        from mcp.mcp_server import MCPToolServer, create_default_tools
        from mcp.order_repository import OrderRepository
        from mcp.tool_execution import ToolExecutor
        from refunds.service import RefundService
        from tickets.service import TicketService
        repository = OrderRepository(order_db)
        # The ledger IS the recovery authority for confirmed writes: without it
        # every confirmed write is refused with execution_ledger_required, and
        # /internal/operation_status has nothing to answer from.
        ledger = ExecutionLedger(repository.db_path)
        app.state.order_repository = repository
        app.state.execution_ledger = ledger
        app.state.tool_executor = ToolExecutor(create_default_tools(
            MCPToolServer(),
            order_repository=repository,
            refund_service=RefundService(repository),
            ticket_service=TicketService(repository),
        ), ledger=ledger)
    from memory.user_memory import UserMemoryService
    memory = UserMemoryService(database=db)
    await memory.initialize()
    app.state.user_memory_service = memory
    # Fail fast instead of degrading silently. With live writes enabled the
    # two-phase flow writes a real pending_action through the platform
    # database, and a runtime that is pointed at the wrong schema would just
    # skip it — surfacing much later as an inexplicable refusal (Phase 6b).
    if os.environ.get("SMARTCS_WRITE_MODE") == "live":
        def _probe(_connection, cursor):
            for table in ("platform_user", "conversation_session", "agent_run_receipt", "pending_action"):
                cursor.execute("SELECT 1 FROM " + table + " LIMIT 1")
            return True
        try:
            await db._call(_probe)
        except Exception as exc:
            raise RuntimeError(
                "SMARTCS_WRITE_MODE=live requires the platform schema in MYSQL_DATABASE="
                + os.environ.get("MYSQL_DATABASE", "?") + " (platform_user/conversation_session/"
                + "agent_run_receipt/pending_action): " + repr(exc)
            ) from exc

uvicorn.run(app, host="127.0.0.1", port=int(os.environ["PHASE1_TEST_PORT"]), log_level="error")
`;
  const child: ChildProcess = spawn(pythonExecutable(), ["-c", program], {
    cwd: PYTHON_IMPL,
    env: {
      ...process.env,
      ...env,
      PYTHONPATH: PYTHON_IMPL,
      // Resolved here, handed over explicitly: process env first, then
      // python-impl/.env (see childMysqlEnv). A clean shell must not be able to
      // start a runtime that cannot reach its platform database.
      ...childMysqlEnv(options.database ?? TEST_DATABASE),
      PHASE1_TEST_PORT: String(options.port),
      INTERNAL_SERVICE_JWT_SECRET: process.env.INTERNAL_SERVICE_JWT_SECRET ?? env.INTERNAL_SERVICE_JWT_SECRET ?? "",
      ...(options.orderDbPath ? { SMARTCS_TEST_ORDER_DB: options.orderDbPath } : {}),
      // Pinned, never inherited: a developer's shell must not be able to turn
      // a test run into a live-write run by accident.
      SMARTCS_WRITE_MODE: options.writeMode ?? "off",
      ...(options.traceRecords ? { SMARTCS_INTERNAL_TRACE_RECORDS: "1" } : {}),
    },
    stdio: ["ignore", "pipe", "pipe"],
  });

  let stderrText = "";
  child.stderr?.on("data", (chunk) => {
    stderrText += String(chunk);
  });

  const url = `http://127.0.0.1:${options.port}`;
  const deadline = Date.now() + 30_000;
  for (;;) {
    // A service that refused to start (bad credentials, missing platform
    // schema) must fail here in milliseconds, with its stderr — not after the
    // full 30s deadline as a generic "did not start".
    if (child.exitCode !== null || child.signalCode !== null) {
      throw new Error(`python internal_api exited during startup (code=${child.exitCode}): ${stderrText}`);
    }
    try {
      const probe = await fetch(`${url}/openapi.json`, { signal: AbortSignal.timeout(1_000) });
      if (probe.ok) break;
    } catch {
      /* not up yet */
    }
    if (Date.now() > deadline) {
      child.kill();
      throw new Error(`python identity server failed to start: ${stderrText}`);
    }
    await new Promise((r) => setTimeout(r, 150));
  }

  return {
    port: options.port,
    url,
    stderr: () => stderrText,
    stop: () =>
      new Promise<void>((resolveStop) => {
        child.once("exit", () => resolveStop());
        child.kill();
        setTimeout(() => {
          if (!child.killed) child.kill("SIGKILL");
          resolveStop();
        }, 3_000).unref?.();
      }),
  };
}

/** Locate the repo's python interpreter without assuming the name. */
export function pythonExecutable(): string {
  return process.env.PYTHON ?? "python";
}

export { PI_HARNESS_ROOT };
