/**
 * shadow_write_plan — the single source of truth for the Phase 4 comparison
 * (phase4-design.md §3).
 *
 * Lives in the harness, not in `python-impl/migrations`: shadow mode is a
 * harness-side capability, and Phase 4 must not touch the Python service. The
 * table lives in the same test database the harness already uses.
 *
 * Every intercepted write attempt inserts exactly one row. Rows are never
 * updated — a shadow plan is an observation, not a state machine.
 */

import type { RowDataPacket } from "mysql2/promise";
import type { Database } from "./mysql.js";

export const SHADOW_PLAN_DDL = `CREATE TABLE IF NOT EXISTS shadow_write_plan (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  session_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
  client_request_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
  tool_name VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
  arguments JSON NOT NULL,
  turn_index INT NOT NULL DEFAULT 0,
  user_message_excerpt VARCHAR(200) NOT NULL DEFAULT '',
  created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  KEY idx_shadow_session (session_id, client_request_id),
  KEY idx_shadow_tool (tool_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`;

export interface ShadowPlanRecord {
  sessionId: string;
  clientRequestId: string;
  toolName: string;
  arguments: Record<string, unknown>;
  turnIndex: number;
  userMessageExcerpt: string;
}

export interface ShadowPlanRow extends ShadowPlanRecord {
  id: number;
  createdAt: string;
}

export class ShadowPlanStore {
  constructor(private readonly db: Database) {}

  /** Idempotent: the table is created by the harness on first use. */
  async ensureTable(): Promise<void> {
    await this.db.execute(SHADOW_PLAN_DDL);
  }

  async record(plan: ShadowPlanRecord): Promise<number> {
    const result = await this.db.execute(
      `INSERT INTO shadow_write_plan
         (session_id, client_request_id, tool_name, arguments, turn_index, user_message_excerpt)
       VALUES (?, ?, ?, CAST(? AS JSON), ?, ?)`,
      [
        plan.sessionId,
        plan.clientRequestId,
        plan.toolName,
        JSON.stringify(plan.arguments ?? {}),
        plan.turnIndex,
        plan.userMessageExcerpt.slice(0, 200),
      ],
    );
    return result.insertId;
  }

  async listFor(sessionId: string): Promise<ShadowPlanRow[]> {
    const rows = await this.db.query<RowDataPacket>(
      "SELECT * FROM shadow_write_plan WHERE session_id = ? ORDER BY id ASC",
      [sessionId],
    );
    return rows.map((row) => ({
      id: Number(row.id),
      sessionId: String(row.session_id),
      clientRequestId: String(row.client_request_id),
      toolName: String(row.tool_name),
      arguments:
        typeof row.arguments === "string"
          ? (JSON.parse(row.arguments) as Record<string, unknown>)
          : ((row.arguments ?? {}) as Record<string, unknown>),
      turnIndex: Number(row.turn_index ?? 0),
      userMessageExcerpt: String(row.user_message_excerpt ?? ""),
      createdAt: String(row.created_at),
    }));
  }

  async countAll(): Promise<number> {
    const rows = await this.db.query<RowDataPacket>("SELECT COUNT(*) AS n FROM shadow_write_plan");
    return Number(rows[0]?.n ?? 0);
  }
}
