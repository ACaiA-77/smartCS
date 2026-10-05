/**
 * Operator CLI for the memory outbox (Phase 11 §9).
 *
 *   npm run outbox:status
 *   npm run outbox:retry -- --receipt-id 123
 *   npm run outbox:retry -- --failed --limit 10
 *
 * WHY THIS FILE IMPORTS ALMOST NOTHING
 *
 * Automatic delivery parks a row as `failed` after `maxAttempts` — a poison
 * message must not spin forever. That leaves a human needing to say "this one
 * was a transient runtime outage; try it again". The recovery path must stay
 * SINGLE: DB state → dispatcher → Business Runtime.
 *
 * So this CLI touches the database and nothing else. It does not import the
 * Python client, the agent, the tool shells or the dispatcher; it cannot
 * enqueue a memory, run a model or call a tool, because the only thing it can
 * do is move a row from `failed` back to `pending` and reset its attempt
 * counter. The ordinary dispatcher then picks it up on its next pass, exactly
 * as if the failure had never been parked.
 */

import { createDatabase } from "../db/mysql.js";
import { ReceiptStore } from "../db/receipts.js";
import { parseArgs, UsageError, type ParsedArgs } from "./outbox-args.js";

const USAGE = `SmartCS memory outbox — operator CLI

Usage:
  npm run outbox:status
      Durable outbox state: pending / failed counts, oldest pending age,
      and the highest attempt counter among pending rows.

  npm run outbox:retry -- --receipt-id <id>
      Return ONE parked receipt to the dispatcher (failed -> pending,
      attempts = 0).

  npm run outbox:retry -- --failed [--limit <n>]
      Return parked receipts, oldest first (default limit 10).

Both retry forms only move a row. Delivery happens afterwards, in the running
dispatcher, over the normal channel — this CLI never calls the model, a tool or
the Business Runtime.

Exit codes: 0 success · 1 error or a targeted receipt that could not be
requeued · 2 usage error.`;

async function status(receipts: ReceiptStore): Promise<number> {
  const stats = await receipts.memoryOutboxStats();
  console.log(`pending: ${stats.pending}`);
  console.log(`failed: ${stats.failed}`);
  console.log(`oldest pending: ${stats.oldestPendingAgeSeconds}s`);
  console.log(`max pending attempts: ${stats.maxPendingAttempts}`);
  return 0;
}

async function retry(receipts: ReceiptStore, args: ParsedArgs): Promise<number> {
  if (args.receiptId !== undefined) {
    const moved = await receipts.requeueMemory(args.receiptId);
    if (!moved) {
      console.error(`receipt ${args.receiptId}: not requeued (not a completed receipt in the failed state)`);
      return 1;
    }
    console.log(`receipt ${args.receiptId}: failed -> pending (attempts reset to 0)`);
    return 0;
  }

  const candidates = await receipts.listFailedMemory(args.limit);
  if (!candidates.length) {
    console.log("no failed receipts");
    return 0;
  }
  let moved = 0;
  for (const candidate of candidates) {
    // Re-read the state indirectly through the conditional UPDATE: another
    // operator (or a dispatcher pass) may have moved the row in between.
    if (await receipts.requeueMemory(candidate.receiptId)) {
      moved += 1;
      console.log(`receipt ${candidate.receiptId}: failed -> pending (attempts reset to 0)`);
    } else {
      console.log(`receipt ${candidate.receiptId}: skipped (no longer failed)`);
    }
  }
  console.log(`requeued ${moved} of ${candidates.length}`);
  return 0;
}

async function main(): Promise<number> {
  let args: ParsedArgs;
  try {
    args = parseArgs(process.argv.slice(2));
  } catch (error) {
    if (error instanceof UsageError) {
      console.error(error.message);
      console.error("");
      console.error(USAGE);
      return 2;
    }
    throw error;
  }

  const db = createDatabase();
  try {
    const receipts = new ReceiptStore(db);
    return args.command === "status" ? await status(receipts) : await retry(receipts, args);
  } finally {
    await db.close();
  }
}

main()
  .then((code) => process.exit(code))
  .catch((error) => {
    // Configuration or connection failure. The message names the problem (e.g.
    // a missing MYSQL_PASSWORD) without dumping a driver stack at the operator.
    console.error(`outbox CLI failed: ${String((error as Error)?.message ?? error)}`);
    process.exit(1);
  });
