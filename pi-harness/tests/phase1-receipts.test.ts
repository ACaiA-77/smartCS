/**
 * Receipt state machine against real MySQL (design §3).
 *
 * Covers: new request, replay on same hash, 409 on different hash, orphan
 * reclaim from `processing`, retry from `failed_recoverable`, and the CAS
 * behaviour that makes concurrent reclaim safe.
 */

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { ReceiptStore, hashRequest } from "../src/db/receipts.js";
import type { Database } from "../src/db/mysql.js";
import { resetTestDatabase, testDatabase } from "./helpers/phase1.js";

const SESSION = "phase1-receipts-session";

describe("agent_run_receipt state machine (real MySQL)", () => {
  let db: Database;
  let store: ReceiptStore;

  beforeAll(async () => {
    await resetTestDatabase();
    db = testDatabase();
    store = new ReceiptStore(db);
  });

  afterAll(async () => {
    await db?.close();
  });

  it("a brand-new request runs and can be completed", async () => {
    const decision = await store.beginRequest(SESSION, "req-new", hashRequest("你好"));
    expect(decision.action).toBe("run");
    if (decision.action !== "run") return;
    expect(decision.reclaimedFrom).toBe("none");

    expect(await store.complete(decision.receiptId, { message: { role: "assistant", content: "答复" } })).toBe(true);
    const row = await store.get(decision.receiptId);
    expect(row?.status).toBe("completed");
    expect((row?.response as { message: { content: string } }).message.content).toBe("答复");
  });

  it("replays a completed receipt with the same hash and does not rerun it", async () => {
    const hash = hashRequest("查订单");
    const first = await store.beginRequest(SESSION, "req-replay", hash);
    if (first.action !== "run") throw new Error("expected run");
    await store.complete(first.receiptId, { message: { role: "assistant", content: "原始答复" } });

    const second = await store.beginRequest(SESSION, "req-replay", hash);
    expect(second.action).toBe("replay");
    if (second.action !== "replay") return;
    expect((second.response as { message: { content: string } }).message.content).toBe("原始答复");
    // Same row — no second receipt was created.
    expect(second.receiptId).toBe(first.receiptId);
  });

  it("rejects the same client_request_id with a different payload (409)", async () => {
    const first = await store.beginRequest(SESSION, "req-conflict", hashRequest("原始内容"));
    if (first.action !== "run") throw new Error("expected run");
    await store.complete(first.receiptId, { message: { role: "assistant", content: "ok" } });

    const conflict = await store.beginRequest(SESSION, "req-conflict", hashRequest("被篡改的内容"));
    expect(conflict.action).toBe("conflict");
  });

  it("reclaims an orphaned processing receipt and reruns exactly once", async () => {
    const hash = hashRequest("崩溃前的问题");
    const first = await store.beginRequest(SESSION, "req-orphan", hash);
    if (first.action !== "run") throw new Error("expected run");
    // Simulate a crash: the receipt is left in `processing` by force.
    await store.forceStatus(first.receiptId, "processing");

    const retry = await store.beginRequest(SESSION, "req-orphan", hash);
    expect(retry.action).toBe("run");
    if (retry.action !== "run") return;
    expect(retry.reclaimedFrom).toBe("processing_orphan");
    expect(retry.receiptId).toBe(first.receiptId);
  });

  it("reclaims a failed_recoverable receipt", async () => {
    const hash = hashRequest("上次失败的请求");
    const first = await store.beginRequest(SESSION, "req-failed", hash);
    if (first.action !== "run") throw new Error("expected run");
    await store.failRecoverable(first.receiptId, "boom");

    const retry = await store.beginRequest(SESSION, "req-failed", hash);
    expect(retry.action).toBe("run");
    if (retry.action !== "run") return;
    expect(retry.reclaimedFrom).toBe("failed_recoverable");
  });

  it("BOUNDARY: concurrent receipts cannot both COMMIT — the second complete() is refused", async () => {
    const hash = hashRequest("并发重发");
    const first = await store.beginRequest(SESSION, "req-race", hash);
    if (first.action !== "run") throw new Error("expected run");
    await store.forceStatus(first.receiptId, "processing");

    // Two callers race the orphan. This is exactly the interleaving the
    // SessionRegistry exists to prevent; the receipt layer's own backstop is
    // that `complete()` is a conditional UPDATE, so at most one caller can
    // ever publish a response. The pipeline maps the loser's `false` to 409.
    const [a, b] = await Promise.all([
      store.beginRequest(SESSION, "req-race", hash),
      store.beginRequest(SESSION, "req-race", hash),
    ]);

    const runners = [a, b].filter((d): d is Extract<typeof a, { action: "run" }> => d.action === "run");
    expect(runners.length).toBeGreaterThanOrEqual(1);

    const completions = await Promise.all(
      runners.map((r) => store.complete(r.receiptId, { message: { role: "assistant", content: r.receiptId.toString() } })),
    );
    expect(completions.filter(Boolean)).toHaveLength(1);

    const row = await store.get(first.receiptId);
    expect(row?.status).toBe("completed");
  });

  it("complete() refuses to overwrite a receipt that is no longer processing", async () => {
    const first = await store.beginRequest(SESSION, "req-late", hashRequest("x"));
    if (first.action !== "run") throw new Error("expected run");
    await store.complete(first.receiptId, { message: { role: "assistant", content: "first" } });
    // A second completion must not clobber the stored response.
    expect(await store.complete(first.receiptId, { message: { role: "assistant", content: "second" } })).toBe(false);
    const row = await store.get(first.receiptId);
    expect((row?.response as { message: { content: string } }).message.content).toBe("first");
  });
});
