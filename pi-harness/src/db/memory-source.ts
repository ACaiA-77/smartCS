/**
 * memory_source_event — the minimal durable provenance ledger (plan v2 §6.6).
 *
 * Only USER_MESSAGE text is recorded, and it is written BEFORE any LLM activity
 * so a crash can never leave a model turn whose source message is unaccounted
 * for. Assistant/tool text never enters this table.
 */

import type { RowDataPacket } from "mysql2/promise";
import type { Database } from "./mysql.js";

export interface MemorySourceEvent {
  eventId: string;
  sessionId: string;
  businessUserId: string;
  clientRequestId: string;
  content: string;
}

export class MemorySourceStore {
  constructor(private readonly db: Database) {}

  /**
   * Durably record the raw user message. Idempotent per
   * (session_id, client_request_id): replaying a request reuses the first row
   * instead of duplicating provenance.
   */
  async record(event: MemorySourceEvent): Promise<void> {
    await this.db.execute(
      `INSERT INTO memory_source_event
         (event_id, session_id, business_user_id, client_request_id, content)
       VALUES (?, ?, ?, ?, ?)
       ON DUPLICATE KEY UPDATE event_id = event_id`,
      [event.eventId, event.sessionId, event.businessUserId, event.clientRequestId, event.content],
    );
  }

  async find(sessionId: string, clientRequestId: string): Promise<MemorySourceEvent | undefined> {
    const rows = await this.db.query<RowDataPacket>(
      `SELECT * FROM memory_source_event WHERE session_id = ? AND client_request_id = ?`,
      [sessionId, clientRequestId],
    );
    if (!rows.length) return undefined;
    const row = rows[0]!;
    return {
      eventId: String(row.event_id),
      sessionId: String(row.session_id),
      businessUserId: String(row.business_user_id),
      clientRequestId: String(row.client_request_id),
      content: String(row.content),
    };
  }

  async count(sessionId: string): Promise<number> {
    const rows = await this.db.query<RowDataPacket>(
      "SELECT COUNT(*) AS n FROM memory_source_event WHERE session_id = ?",
      [sessionId],
    );
    return Number(rows[0]?.n ?? 0);
  }

  /** Mark provenance for a deleted session; rows are retained for audit. */
  async markCleared(sessionId: string): Promise<number> {
    const result = await this.db.execute(
      "UPDATE memory_source_event SET cleared_at = CURRENT_TIMESTAMP(3) WHERE session_id = ? AND cleared_at IS NULL",
      [sessionId],
    );
    return result.affectedRows;
  }
}
