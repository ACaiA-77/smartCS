/**
 * Durable memory outbox dispatcher (plan v2 §6.6, phase3-design.md §3).
 *
 * The user's raw message is already durable in `memory_source_event` before the
 * model ever runs; this loop is what eventually turns those rows into memory
 * candidates. Because the state lives on the receipt, a crash (including
 * kill -9) simply means the row is still `pending` when the next process scans,
 * so delivery happens exactly once even across restarts.
 *
 * Failure policy: count the attempt, retry on the next tick, and park the row
 * as `failed` after the threshold. A memory failure never replays a model turn
 * or a tool call.
 */

import type { ReceiptStore } from "../db/receipts.js";
import type { PythonInternalClient } from "../business/python-client.js";
import type { TurnIdentity } from "../business/turn-context.js";

export interface OutboxDeps {
  receipts: ReceiptStore;
  pythonClient: PythonInternalClient;
  /** Resolve the identity a receipt belongs to (the harness does not store it). */
  identityFor: (sessionId: string) => TurnIdentity | undefined;
  batchSize?: number;
  maxAttempts?: number;
  intervalMs?: number;
}

export interface DispatchSummary {
  scanned: number;
  enqueued: number;
  failed: number;
  skipped: number;
}

export class MemoryOutboxDispatcher {
  private timer: NodeJS.Timeout | undefined;
  private running = false;

  constructor(private readonly deps: OutboxDeps) {}

  /** One bounded pass. Safe to call directly from tests. */
  async dispatchOnce(): Promise<DispatchSummary> {
    const summary: DispatchSummary = { scanned: 0, enqueued: 0, failed: 0, skipped: 0 };
    const batch = await this.deps.receipts.listPendingMemory(this.deps.batchSize ?? 20);
    summary.scanned = batch.length;

    for (const item of batch) {
      const identity = this.deps.identityFor(item.sessionId);
      if (!identity) {
        // The session is not resident (idle-evicted or another process owns it).
        // Leaving the receipt pending is correct: a later tick will pick it up.
        summary.skipped += 1;
        continue;
      }
      const sourceEventId = await this.deps.receipts.sourceEventId(item.sessionId, item.clientRequestId);
      if (!sourceEventId) {
        // No provenance row means there is nothing legitimate to enqueue.
        await this.deps.receipts.recordMemoryFailure(item.receiptId, this.deps.maxAttempts ?? 5);
        summary.failed += 1;
        continue;
      }

      try {
        const result = await this.deps.pythonClient.enqueueMemory({
          identity: { ...identity, clientRequestId: item.clientRequestId },
          sourceEventId,
        });
        if (result.enqueued) {
          await this.deps.receipts.markMemoryDone(item.receiptId);
          summary.enqueued += 1;
        } else {
          await this.deps.receipts.recordMemoryFailure(item.receiptId, this.deps.maxAttempts ?? 5);
          summary.failed += 1;
        }
      } catch {
        await this.deps.receipts.recordMemoryFailure(item.receiptId, this.deps.maxAttempts ?? 5);
        summary.failed += 1;
      }
    }
    return summary;
  }

  start(): void {
    if (this.timer) return;
    const interval = this.deps.intervalMs ?? 5_000;
    this.timer = setInterval(() => {
      if (this.running) return; // never overlap passes
      this.running = true;
      void this.dispatchOnce()
        .catch(() => undefined)
        .finally(() => {
          this.running = false;
        });
    }, interval);
    this.timer.unref?.();
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = undefined;
  }
}
