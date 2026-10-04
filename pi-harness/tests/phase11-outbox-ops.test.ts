/**
 * Phase 11 §6.2/§9 — the operator surface, offline half.
 *
 *   P11-O1  the dispatcher's process counters track passes/deliveries/failures
 *           and are explicitly non-authoritative (they reset on restart)
 *   P11-O2  the CLI argument surface: what it accepts, and what it refuses
 *
 * FULLY OFFLINE: the dispatcher is driven through a hand-written receipt stub
 * and the CLI's parser is pure — no MySQL, no Python, no model. The DB half of
 * the operator surface (the aggregate query, the replay, the ops endpoint, the
 * real CLI process) lives in phase11-outbox-ops-db.test.ts.
 */

import { describe, expect, it } from "vitest";
import { MemoryOutboxDispatcher } from "../src/session/outbox.js";
import { parseArgs } from "../src/cli/outbox-args.js";
import type { ReceiptStore } from "../src/db/receipts.js";
import type { PythonInternalClient } from "../src/business/python-client.js";

/** The narrow slice of ReceiptStore the dispatcher actually calls. */
interface StubReceipts {
  listPendingMemory: ReceiptStore["listPendingMemory"];
  resolveMemoryContext: ReceiptStore["resolveMemoryContext"];
  markMemoryDone: ReceiptStore["markMemoryDone"];
  recordMemoryFailure: ReceiptStore["recordMemoryFailure"];
}

function dispatcherOver(stub: StubReceipts, options: { enqueue?: boolean } = {}) {
  const client = {
    enqueueMemory: async () => ({ enqueued: options.enqueue ?? true, result: null }),
  } as unknown as PythonInternalClient;
  return new MemoryOutboxDispatcher({
    receipts: stub as unknown as ReceiptStore,
    pythonClient: client,
    batchSize: 10,
  });
}

const CONTEXT = { accountId: 1, businessUserId: "user_001", sourceEventId: "evt-1" };

describe("Phase 11 — dispatcher process stats", () => {
  it("P11-O1: a healthy pass counts as one pass and one delivery", async () => {
    const dispatcher = dispatcherOver({
      listPendingMemory: async () => [
        { receiptId: 1, sessionId: "s", clientRequestId: "r", attempts: 0 },
      ],
      resolveMemoryContext: async () => CONTEXT,
      markMemoryDone: async () => true,
      recordMemoryFailure: async () => ({ attempts: 1, status: "pending" }),
    });

    expect(dispatcher.stats()).toEqual({
      passes: 0,
      delivered: 0,
      deliveryFailures: 0,
      parked: 0,
      lastDispatchAt: null,
      lastSuccessAt: null,
      lastErrorAt: null,
    });

    const summary = await dispatcher.dispatchOnce();
    expect(summary).toMatchObject({ scanned: 1, enqueued: 1, failed: 0 });

    const stats = dispatcher.stats();
    expect(stats.passes).toBe(1);
    expect(stats.delivered).toBe(1);
    expect(stats.deliveryFailures).toBe(0);
    expect(stats.lastDispatchAt).not.toBeNull();
    expect(stats.lastSuccessAt).not.toBeNull();
    expect(stats.lastErrorAt).toBeNull();
  });

  it("P11-O1b: failures and parks are counted separately from deliveries", async () => {
    const dispatcher = dispatcherOver(
      {
        listPendingMemory: async () => [
          { receiptId: 1, sessionId: "s", clientRequestId: "r1", attempts: 0 },
          { receiptId: 2, sessionId: "s", clientRequestId: "r2", attempts: 4 },
        ],
        resolveMemoryContext: async () => CONTEXT,
        markMemoryDone: async () => false,
        // r2 is on its last attempt: the counter reaching the threshold parks it.
        recordMemoryFailure: async (receiptId: number) =>
          receiptId === 2 ? { attempts: 5, status: "failed" } : { attempts: 1, status: "pending" },
      },
      { enqueue: false },
    );

    const summary = await dispatcher.dispatchOnce();
    expect(summary).toMatchObject({ scanned: 2, enqueued: 0, failed: 2, parked: 1 });

    const stats = dispatcher.stats();
    expect(stats.passes).toBe(1);
    expect(stats.delivered).toBe(0);
    expect(stats.deliveryFailures).toBe(2);
    expect(stats.parked).toBe(1);
    expect(stats.lastErrorAt).not.toBeNull();
    expect(stats.lastSuccessAt).toBeNull();
  });

  it("P11-O1c: a pass that cannot even read the queue is not counted as a pass", async () => {
    const dispatcher = dispatcherOver({
      listPendingMemory: async () => {
        throw new Error("mysql gone");
      },
      resolveMemoryContext: async () => CONTEXT,
      markMemoryDone: async () => true,
      recordMemoryFailure: async () => ({ attempts: 1, status: "pending" }),
    });

    await expect(dispatcher.dispatchOnce()).rejects.toThrow("mysql gone");
    const stats = dispatcher.stats();
    expect(stats.passes).toBe(0);
    expect(stats.lastDispatchAt).toBeNull();
    expect(stats.lastErrorAt).not.toBeNull();
  });
});

describe("Phase 11 — outbox CLI arguments", () => {
  it("P11-O2: accepts exactly the documented forms", () => {
    expect(parseArgs(["status"])).toEqual({ command: "status", failed: false, limit: 10 });
    expect(parseArgs(["retry", "--receipt-id", "123"])).toEqual({
      command: "retry",
      receiptId: 123,
      failed: false,
      limit: 10,
    });
    expect(parseArgs(["retry", "--failed"])).toEqual({
      command: "retry",
      receiptId: undefined,
      failed: true,
      limit: 10,
    });
    expect(parseArgs(["retry", "--failed", "--limit", "5"])).toMatchObject({ failed: true, limit: 5 });
  });

  it("P11-O2b: refuses anything ambiguous or malformed", () => {
    // No selection at all: a bare `retry` would have to guess.
    expect(() => parseArgs(["retry"])).toThrow(/receipt-id|--failed/);
    // Both selections at once: contradictory.
    expect(() => parseArgs(["retry", "--receipt-id", "1", "--failed"])).toThrow(/mutually exclusive/);
    expect(() => parseArgs(["retry", "--receipt-id", "abc"])).toThrow(/positive integer/);
    expect(() => parseArgs(["retry", "--receipt-id", "-5"])).toThrow(/positive integer/);
    expect(() => parseArgs(["retry", "--failed", "--limit", "0"])).toThrow(/positive integer/);
    expect(() => parseArgs(["retry", "--failed", "--limit"])).toThrow(/positive integer/);
    expect(() => parseArgs(["retry", "--failed", "--wat"])).toThrow(/unknown option/);
    expect(() => parseArgs([])).toThrow(/unknown command/);
    expect(() => parseArgs(["flush"])).toThrow(/unknown command/);
  });
});
