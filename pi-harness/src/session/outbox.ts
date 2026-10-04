/**
 * Durable memory outbox dispatcher (plan v2 §6.6, phase3-design.md §3).
 *
 * The user's raw message is already durable in `memory_source_event` before the
 * model ever runs; this loop is what eventually turns those rows into memory
 * candidates. Because the state lives on the receipt, a crash (including
 * kill -9) simply means the row is still `pending` when the next process scans,
 * so delivery happens exactly once even across restarts.
 *
 * IDENTITY IS RECOVERED FROM DURABLE ROWS, NEVER FROM PROCESS MEMORY.
 *
 * An earlier version asked the caller for `identityFor(sessionId)` and read the
 * resident `TurnContext`. That is unavailable exactly when the outbox matters:
 * the turn has ended (the context is cleared), the session was idle-evicted, or
 * the process was restarted. Delivery now rebuilds the identity per row from
 * `conversation_session` + `memory_source_event` and mints a *fresh* service
 * token for it, so nothing in this file depends on live Node state. The
 * principle is the one the runtime enforces on the other side: the durable
 * ledger is the authority, the process is not.
 *
 * Failure policy: count the attempt, retry on the next tick, and park the row
 * as `failed` after the threshold. A memory failure never replays a model turn
 * or a tool call — this loop has no access to a session and no way to run one.
 */

import { hitCrashPoint } from "../test-support/crash-point.js";
import type { ReceiptStore } from "../db/receipts.js";
import type { PythonInternalClient } from "../business/python-client.js";
import type { TurnIdentity } from "../business/turn-context.js";

export interface OutboxDeps {
  receipts: ReceiptStore;
  pythonClient: PythonInternalClient;
  batchSize?: number;
  maxAttempts?: number;
  intervalMs?: number;
  /** Delivery failures (transport, runtime refusal, missing provenance). */
  onError?: (error: unknown) => void;
}

export interface DispatchSummary {
  scanned: number;
  enqueued: number;
  failed: number;
  /** Rows that reached `maxAttempts` on this pass and were parked as `failed`. */
  parked: number;
}

export class MemoryOutboxDispatcher {
  private timer: NodeJS.Timeout | undefined;
  private inFlight: Promise<DispatchSummary> | undefined;

  constructor(private readonly deps: OutboxDeps) {}

  /**
   * One bounded pass. Safe to call directly from tests.
   *
   * Every row is handled independently: one unrecoverable row must not abort
   * the pass and starve the rest of the batch.
   */
  async dispatchOnce(): Promise<DispatchSummary> {
    const summary: DispatchSummary = { scanned: 0, enqueued: 0, failed: 0, parked: 0 };
    const batch = await this.deps.receipts.listPendingMemory(this.deps.batchSize ?? 20);
    summary.scanned = batch.length;

    for (const item of batch) {
      try {
        // Durable identity recovery. `undefined` means the ledger no longer
        // holds what a delivery would have to attest to (session row gone, or
        // provenance cleared) — a permanent condition, so it counts as a
        // failure and is eventually parked, never retried forever.
        const context = await this.deps.receipts.resolveMemoryContext(
          item.sessionId,
          item.clientRequestId,
        );
        if (!context) {
          const outcome = await this.deps.receipts.recordMemoryFailure(
            item.receiptId,
            this.deps.maxAttempts ?? 5,
          );
          summary.failed += 1;
          if (outcome.status === "failed") summary.parked += 1;
          this.deps.onError?.(new Error(`no durable memory provenance for receipt ${item.receiptId}`));
          continue;
        }

        const identity: TurnIdentity = {
          accountId: context.accountId,
          businessUserId: context.businessUserId,
          sessionId: item.sessionId,
          clientRequestId: item.clientRequestId,
        };

        // F15 window: the receipt is `completed`, the ledger row is durable,
        // and the runtime has not been told yet. A crash here must leave the
        // row `pending` so the next process delivers it.
        hitCrashPoint("before_memory_enqueue");

        const result = await this.deps.pythonClient.enqueueMemory({
          identity,
          sourceEventId: context.sourceEventId,
        });
        if (result.enqueued) {
          // CAS on `memory_enqueue_status='pending'`: if another pass (or a
          // duplicate process) already delivered this row, this is a no-op and
          // nothing is double-counted. The runtime's candidate insert is
          // idempotent on top of that.
          await this.deps.receipts.markMemoryDone(item.receiptId);
          summary.enqueued += 1;
        } else {
          const outcome = await this.deps.receipts.recordMemoryFailure(
            item.receiptId,
            this.deps.maxAttempts ?? 5,
          );
          summary.failed += 1;
          if (outcome.status === "failed") summary.parked += 1;
        }
      } catch (error) {
        const outcome = await this.deps.receipts
          .recordMemoryFailure(item.receiptId, this.deps.maxAttempts ?? 5)
          .catch(() => undefined);
        summary.failed += 1;
        if (outcome?.status === "failed") summary.parked += 1;
        this.deps.onError?.(error);
      }
    }
    return summary;
  }

  start(): void {
    if (this.timer) return;
    const interval = this.deps.intervalMs ?? 5_000;
    this.timer = setInterval(() => {
      if (this.inFlight) return; // never overlap passes
      const pass = this.dispatchOnce()
        .catch((error) => {
          this.deps.onError?.(error);
          return { scanned: 0, enqueued: 0, failed: 0, parked: 0 } satisfies DispatchSummary;
        })
        .finally(() => {
          if (this.inFlight === pass) this.inFlight = undefined;
        });
      this.inFlight = pass;
    }, interval);
    this.timer.unref?.();
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = undefined;
  }

  /**
   * Graceful shutdown: wait for an in-flight pass, then run one final bounded
   * pass. Unlike the audit queue (best-effort by contract), a memory delivery
   * that is already durable should be attempted before the process goes away.
   * Anything left over is still `pending` and the next process picks it up.
   */
  async flush(): Promise<DispatchSummary> {
    this.stop();
    if (this.inFlight) await this.inFlight.catch(() => undefined);
    return this.dispatchOnce();
  }
}
