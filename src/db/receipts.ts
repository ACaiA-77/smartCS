/**
 * agent_run_receipt repository + request state machine (plan v2 §5.3,
 * phase1-design.md §3).
 *
 *   new request → processing → completed
 *                           ↘ failed_recoverable
 *
 * Phase 1 has NO write tools, so `open_write_operations` is always empty and a
 * `processing` row found at the start of a request is by definition an orphan
 * left by a crashed process — which is why "reclaim and rerun" is safe here.
 * That reasoning expires in Phase 5 (v2 §6.3 takes over).
 *
 * SINGLE-WRITER INVARIANT: reclaiming an orphan is only safe because the
 * caller holds the per-session lease. Without it a live concurrent request
 * would look identical to an orphan and get double-run. SessionRegistry is the
 * only thing providing that guarantee; do not call these methods outside it.
 */

import { createHash } from "node:crypto";
import type { RowDataPacket } from "mysql2/promise";
import type { Database } from "./mysql.js";

export type ReceiptStatus = "processing" | "completed" | "failed_recoverable";

/**
 * Everything the memory outbox needs to deliver one receipt, recovered from
 * durable rows (see `ReceiptStore.resolveMemoryContext`). No field here may
 * ever be sourced from a Node-side cache.
 */
export interface MemoryDeliveryContext {
  /** From `conversation_session.account_id` — the owner the runtime bound. */
  accountId: number;
  /** From `memory_source_event.business_user_id` — recorded before the model ran. */
  businessUserId: string;
  /** The provenance row's event id, the runtime's own verification key. */
  sourceEventId: string;
}

export interface ReceiptRow {
  id: number;
  session_id: string;
  client_request_id: string;
  request_hash: string;
  status: ReceiptStatus;
  response: unknown;
  open_write_operations: unknown;
  pi_revision_note: string | null;
}

export type ReceiptDecision =
  | { action: "replay"; response: unknown; receiptId: number }
  | { action: "conflict"; detail: string }
  | {
      action: "run";
      receiptId: number;
      reclaimedFrom: "none" | "failed_recoverable" | "processing_orphan";
    };

/** Canonical request hash: sha256 over the exact user message text. */
export function hashRequest(message: string): string {
  return createHash("sha256").update(message, "utf8").digest("hex");
}

function isDuplicateKey(error: unknown): boolean {
  return (error as { code?: string } | null)?.code === "ER_DUP_ENTRY";
}

function parseJsonColumn(value: unknown): unknown {
  if (value === null || value === undefined) return null;
  if (typeof value === "string") {
    try {
      return JSON.parse(value);
    } catch {
      return null;
    }
  }
  return value;
}

function toRow(raw: RowDataPacket): ReceiptRow {
  return {
    id: Number(raw.id),
    session_id: String(raw.session_id),
    client_request_id: String(raw.client_request_id),
    request_hash: String(raw.request_hash),
    status: raw.status as ReceiptStatus,
    response: parseJsonColumn(raw.response),
    open_write_operations: parseJsonColumn(raw.open_write_operations),
    pi_revision_note: raw.pi_revision_note === null ? null : String(raw.pi_revision_note),
  };
}

export class ReceiptStore {
  constructor(private readonly db: Database) {}

  private async readById(id: number): Promise<ReceiptRow | undefined> {
    const rows = await this.db.query<RowDataPacket>("SELECT * FROM agent_run_receipt WHERE id = ?", [id]);
    return rows.length ? toRow(rows[0]!) : undefined;
  }

  private async select(sessionId: string, clientRequestId: string): Promise<ReceiptRow | undefined> {
    const rows = await this.db.query<RowDataPacket>(
      "SELECT * FROM agent_run_receipt WHERE session_id = ? AND client_request_id = ?",
      [sessionId, clientRequestId],
    );
    return rows.length ? toRow(rows[0]!) : undefined;
  }

  /**
   * Insert-if-absent, then apply the §3 lookup table.
   * Returns what the caller is allowed to do next.
   *
   * A row we insert on this call is OURS and runs immediately — it must not be
   * mistaken for an orphan (a naive "read back the row and see `processing`"
   * would classify every first request as a crash residue).
   */
  async beginRequest(
    sessionId: string,
    clientRequestId: string,
    requestHash: string,
  ): Promise<ReceiptDecision> {
    for (let attempt = 0; attempt < 4; attempt += 1) {
      const existing = await this.select(sessionId, clientRequestId);

      if (!existing) {
        try {
          const inserted = await this.db.execute(
            `INSERT INTO agent_run_receipt
               (session_id, client_request_id, request_hash, status, open_write_operations)
             VALUES (?, ?, ?, 'processing', JSON_ARRAY())`,
            [sessionId, clientRequestId, requestHash],
          );
          return { action: "run", receiptId: inserted.insertId, reclaimedFrom: "none" };
        } catch (error) {
          // Someone inserted between our SELECT and INSERT (or an earlier row
          // survived a failed transaction). Re-read and take the normal path.
          if (isDuplicateKey(error)) continue;
          throw error;
        }
      }

      const row = existing;

      if (row.request_hash !== requestHash) {
        // Same request id, different payload: never guess which one is meant.
        return { action: "conflict", detail: "client_request_id reused with a different payload" };
      }

      if (row.status === "completed") {
        return { action: "replay", response: row.response, receiptId: row.id };
      }

      if (row.status === "failed_recoverable") {
        // Conditional update is a natural CAS: only one caller wins.
        const claimed = await this.db.execute(
          `UPDATE agent_run_receipt SET status='processing', pi_revision_note=NULL
             WHERE id = ? AND status = 'failed_recoverable'`,
          [row.id],
        );
        if (claimed.affectedRows === 1) {
          return { action: "run", receiptId: row.id, reclaimedFrom: "failed_recoverable" };
        }
        continue; // someone else moved it; re-read
      }

      // status === "processing": with no write tools this is a crashed run.
      const marked = await this.db.execute(
        `UPDATE agent_run_receipt
            SET status='failed_recoverable',
                pi_revision_note='reclaimed orphan processing receipt'
          WHERE id = ? AND status = 'processing'`,
        [row.id],
      );
      if (marked.affectedRows !== 1) continue; // raced; re-read

      const reclaimed = await this.db.execute(
        `UPDATE agent_run_receipt SET status='processing'
           WHERE id = ? AND status = 'failed_recoverable'`,
        [row.id],
      );
      if (reclaimed.affectedRows === 1) {
        return { action: "run", receiptId: row.id, reclaimedFrom: "processing_orphan" };
      }
      continue;
    }

    return { action: "conflict", detail: "receipt state kept changing; retry the request" };
  }

  async complete(receiptId: number, response: unknown): Promise<boolean> {
    const result = await this.db.execute(
      `UPDATE agent_run_receipt SET status='completed', response=CAST(? AS JSON)
         WHERE id = ? AND status = 'processing'`,
      [JSON.stringify(response ?? null), receiptId],
    );
    return result.affectedRows === 1;
  }

  async failRecoverable(receiptId: number, note: string): Promise<boolean> {
    const result = await this.db.execute(
      `UPDATE agent_run_receipt
          SET status='failed_recoverable', pi_revision_note=?
        WHERE id = ? AND status = 'processing'`,
      [note.slice(0, 255), receiptId],
    );
    return result.affectedRows === 1;
  }

  async get(receiptId: number): Promise<ReceiptRow | undefined> {
    return this.readById(receiptId);
  }

  /** Test/diagnostic helper: force a receipt into a given state. */
  async forceStatus(receiptId: number, status: ReceiptStatus): Promise<void> {
    await this.db.execute("UPDATE agent_run_receipt SET status = ? WHERE id = ?", [status, receiptId]);
  }

  // ---------------------------------------------------------------------------
  // Memory outbox (Phase 3, plan v2 §6.6)
  //
  // A completed receipt starts life with memory_enqueue_status='pending'; the
  // dispatcher drains it. Nothing here replays the model or the tools — a failed
  // enqueue is recorded and retried, never turned into a second prompt.
  // ---------------------------------------------------------------------------

  /** Completed runs whose memory has not been enqueued yet (bounded batch). */
  async listPendingMemory(limit = 20): Promise<
    Array<{ receiptId: number; sessionId: string; clientRequestId: string; attempts: number }>
  > {
    const rows = await this.db.query<RowDataPacket>(
      `SELECT id, session_id, client_request_id, memory_attempts
         FROM agent_run_receipt
        WHERE status = 'completed' AND memory_enqueue_status = 'pending'
        ORDER BY updated_at ASC
        LIMIT ?`,
      [limit],
    );
    return rows.map((row) => ({
      receiptId: Number(row.id),
      sessionId: String(row.session_id),
      clientRequestId: String(row.client_request_id),
      attempts: Number(row.memory_attempts ?? 0),
    }));
  }

  /** The durable provenance row written before the model ran. */
  async sourceEventId(sessionId: string, clientRequestId: string): Promise<string | undefined> {
    const rows = await this.db.query<RowDataPacket>(
      "SELECT event_id FROM memory_source_event WHERE session_id = ? AND client_request_id = ?",
      [sessionId, clientRequestId],
    );
    return rows.length ? String(rows[0]!.event_id) : undefined;
  }

  /**
   * Recover everything a memory delivery needs, from durable rows only.
   *
   * The outbox used to read the identity out of the resident `TurnContext`,
   * which is gone the moment the turn ends — and therefore gone after an idle
   * eviction, a restart, or a `kill -9` between completion and enqueue. This is
   * the replacement, and it deliberately touches no Node process state:
   *
   *   receipt.session_id          → conversation_session.account_id
   *   receipt.(session,request)   → memory_source_event.business_user_id + event_id
   *
   * The account is the one the runtime bound the session to when it created it;
   * the business user is the one recorded on the provenance ledger before the
   * model ever ran. Both are authority owned by the runtime, not by this
   * process, so a fresh service token minted from them says exactly what the
   * original turn said.
   *
   * A cleared provenance row is excluded here for the same reason the runtime
   * excludes it: the user asked for that text to be forgotten.
   */
  async resolveMemoryContext(
    sessionId: string,
    clientRequestId: string,
  ): Promise<MemoryDeliveryContext | undefined> {
    const rows = await this.db.query<RowDataPacket>(
      `SELECT cs.account_id       AS account_id,
              mse.business_user_id AS business_user_id,
              mse.event_id         AS event_id
         FROM agent_run_receipt r
         JOIN conversation_session cs ON cs.session_id = r.session_id
         JOIN memory_source_event mse
              ON mse.session_id = r.session_id AND mse.client_request_id = r.client_request_id
        WHERE r.session_id = ? AND r.client_request_id = ?
          AND mse.cleared_at IS NULL
        LIMIT 1`,
      [sessionId, clientRequestId],
    );
    if (!rows.length) return undefined;
    const row = rows[0]!;
    return {
      accountId: Number(row.account_id),
      businessUserId: String(row.business_user_id),
      sourceEventId: String(row.event_id),
    };
  }

  async markMemoryDone(receiptId: number): Promise<boolean> {
    const result = await this.db.execute(
      `UPDATE agent_run_receipt SET memory_enqueue_status='done'
         WHERE id = ? AND memory_enqueue_status = 'pending'`,
      [receiptId],
    );
    return result.affectedRows === 1;
  }

  /**
   * Count a failed attempt. Beyond `maxAttempts` the row is parked as `failed`
   * so a poison event cannot spin forever; it stays visible for audit.
   */
  async recordMemoryFailure(receiptId: number, maxAttempts = 5): Promise<{"attempts": number; status: string}> {
    await this.db.execute(
      "UPDATE agent_run_receipt SET memory_attempts = memory_attempts + 1 WHERE id = ?",
      [receiptId],
    );
    const rows = await this.db.query<RowDataPacket>(
      "SELECT memory_attempts FROM agent_run_receipt WHERE id = ?",
      [receiptId],
    );
    const attempts = Number(rows[0]?.memory_attempts ?? 0);
    if (attempts >= maxAttempts) {
      await this.db.execute(
        "UPDATE agent_run_receipt SET memory_enqueue_status='failed' WHERE id = ? AND memory_enqueue_status='pending'",
        [receiptId],
      );
      return { attempts, status: "failed" };
    }
    return { attempts, status: "pending" };
  }

  /**
   * The durable write operations recorded for a request (Phase 5).
   *
   * This is how the harness recovers an operation id it never saw: the runtime
   * made it durable BEFORE sending, so even a dropped response leaves a
   * traceable record.
   */
  async openWriteOperations(
    sessionId: string,
    clientRequestId: string,
  ): Promise<Array<{ operation_id: string; tool: string; state: string; target_hash: string }>> {
    const rows = await this.db.query<RowDataPacket>(
      "SELECT open_write_operations FROM agent_run_receipt WHERE session_id = ? AND client_request_id = ?",
      [sessionId, clientRequestId],
    );
    if (!rows.length) return [];
    let operations = rows[0]!.open_write_operations;
    if (typeof operations === "string") {
      try {
        operations = JSON.parse(operations);
      } catch {
        return [];
      }
    }
    return Array.isArray(operations)
      ? operations.map((item) => ({
          operation_id: String((item as { operation_id?: unknown }).operation_id ?? ""),
          tool: String((item as { tool?: unknown }).tool ?? ""),
          state: String((item as { state?: unknown }).state ?? ""),
          target_hash: String((item as { target_hash?: unknown }).target_hash ?? ""),
        }))
      : [];
  }

  /** Test/diagnostic: the outbox state of one receipt. */
  async memoryState(
    sessionId: string,
    clientRequestId: string,
  ): Promise<{ status: string; attempts: number } | undefined> {
    const rows = await this.db.query<RowDataPacket>(
      "SELECT memory_enqueue_status, memory_attempts FROM agent_run_receipt WHERE session_id = ? AND client_request_id = ?",
      [sessionId, clientRequestId],
    );
    if (!rows.length) return undefined;
    return {
      status: String(rows[0]!.memory_enqueue_status),
      attempts: Number(rows[0]!.memory_attempts ?? 0),
    };
  }
}
