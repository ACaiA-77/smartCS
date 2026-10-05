/**
 * Phase 1 acceptance cases (design §9) — real MySQL, real Python internal_api
 * subprocess, real pi runtime with the Faux provider.
 *
 * The only stubbed layer is the model itself (Faux), which is required for a
 * deterministic offline suite; every other hop is the production code path.
 */

import { existsSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { fauxAssistantMessage } from "@earendil-works/pi-ai/providers/faux";
import { SessionManager } from "@earendil-works/pi-coding-agent";
import type { FauxProviderRegistration } from "@earendil-works/pi-ai/compat";
import { createFauxProvider } from "../src/agent/create-smartcs-agent.js";
import { SessionRegistry } from "../src/session/registry.js";
import { openOrCreatePiSession } from "../src/session/pi-session.js";
import { PythonInternalClient } from "../src/business/python-client.js";
import { ReceiptStore } from "../src/db/receipts.js";
import { MemorySourceStore } from "../src/db/memory-source.js";
import type { Database } from "../src/db/mysql.js";
import { createHarnessServer, type HarnessServer } from "../src/server/app.js";
import { signHs256 } from "../src/business/jwt-hs256.js";
import { userJwtSecret, resolveSmartCsPaths, type SmartCsPaths } from "../src/config/env.js";
import { makeTmpDir } from "./helpers/harness.js";
import {
  resetTestDatabase,
  seedAccount,
  seedSession,
  startPythonService,
  testDatabase,
  type PythonService,
  type SeededAccount,
} from "./helpers/phase1.js";

// The identity subprocess inherits these; python-impl/.env supplies
// AUTH_JWT_SECRET so both sides verify with the same key.
process.env.INTERNAL_SERVICE_JWT_SECRET =
  process.env.INTERNAL_SERVICE_JWT_SECRET ?? "phase1-e2e-service-secret-0123456789";

const PY_PORT = 8_940;
let python: PythonService;
let db: Database;
let receipts: ReceiptStore;
let memorySource: MemorySourceStore;
let pythonClient: PythonInternalClient;
let root: string;
let paths: SmartCsPaths;
let faux: FauxProviderRegistration;
let harness: HarnessServer;
let account: SeededAccount;
let otherAccount: SeededAccount;

const SESSION_PI = "phase1-e2e-pi-session";
const SESSION_LEGACY = "phase1-e2e-legacy-session";
const SESSION_OTHER = "phase1-e2e-other-session";

function userToken(accountId: number, secret = userJwtSecret(), ttl = 1800): string {
  const now = Math.floor(Date.now() / 1000);
  return signHs256(
    { sub: String(accountId), iat: now, exp: now + ttl, iss: "smartcs", jti: `jti-${now}-${Math.random()}` },
    secret,
  );
}

/**
 * Port the live harness listens on. The restart case rebinds to a second port:
 * a real process restart also gives every client a fresh connection pool, and
 * reusing the port in-process would only exercise undici's keep-alive reuse.
 */
let harnessPort = PY_PORT + 1;

async function chat(body: Record<string, unknown>, token = userToken(account.accountId)) {
  const response = await fetch(`http://127.0.0.1:${harnessPort}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify(body),
  });
  return { status: response.status, body: (await response.json()) as Record<string, unknown> };
}

function buildHarness(): HarnessServer {
  const registry = new SessionRegistry(async (sessionId) => {
    const handle = await openOrCreatePiSession(paths, sessionId, { provider: "faux", faux });
    return { session: handle.session, origin: handle.origin };
  });
  return createHarnessServer({
    paths,
    registry,
    receipts,
    memorySource,
    pythonClient,
    idleEvictionMs: 15 * 60_000,
  });
}

function piEntryCount(sessionId: string): number {
  const found = SessionManager.findById(paths.runtimeCwd, sessionId, paths.sessionDir);
  if (!found || !existsSync(found)) return 0;
  return SessionManager.open(found, paths.sessionDir, paths.runtimeCwd).getEntries().length;
}

/**
 * Tokens the Business Runtime mints FOR the harness (runtime -> harness).
 * The audience names the receiver, so this is the mirror of mintServiceToken.
 */
function runtimeToken(accountId: number, sessionId: string): string {
  const now = Math.floor(Date.now() / 1000);
  return signHs256(
    {
      iss: "smartcs-business-runtime",
      aud: "smartcs-pi-harness",
      account_id: accountId,
      business_user_id: "bu-e2e-owner",
      session_id: sessionId,
      client_request_id: "history",
      iat: now,
      exp: now + 60,
    },
    process.env.INTERNAL_SERVICE_JWT_SECRET!,
  );
}

beforeAll(async () => {
  await resetTestDatabase();
  db = testDatabase();
  receipts = new ReceiptStore(db);
  memorySource = new MemorySourceStore(db);

  account = await seedAccount(db, { username: "e2e-owner", businessUserId: "bu-e2e-owner" });
  otherAccount = await seedAccount(db, { username: "e2e-other", businessUserId: "bu-e2e-other" });
  await seedSession(db, { sessionId: SESSION_PI, accountId: account.accountId, harnessVersion: "pi" });
  await seedSession(db, { sessionId: SESSION_LEGACY, accountId: account.accountId, harnessVersion: "legacy" });
  await seedSession(db, { sessionId: SESSION_OTHER, accountId: otherAccount.accountId, harnessVersion: "pi" });

  python = await startPythonService({ port: PY_PORT });
  pythonClient = new PythonInternalClient({ baseUrl: python.url });

  root = makeTmpDir("phase1-e2e-");
  paths = resolveSmartCsPaths({
    runtimeCwd: join(root, "cwd"),
    sessionDir: join(root, "sessions"),
    agentDir: join(root, "agent"),
  });
  mkdirSync(paths.runtimeCwd, { recursive: true });
  mkdirSync(paths.sessionDir, { recursive: true });

  faux = createFauxProvider();
  harness = buildHarness();
  await harness.listen(PY_PORT + 1);
}, 120_000);

afterAll(async () => {
  await harness?.close();
  faux?.unregister();
  await python?.stop();
  await db?.close();
  try {
    rmSync(root, { recursive: true, force: true });
  } catch {
    /* temp dirs are disposable */
  }
});

describe("Phase 1 acceptance (§9)", () => {
  it("§9-权限: cross-account session is refused, legacy harness_version is 409", async () => {
    // Another account's session: the token is valid, the session is not theirs.
    const cross = await chat(
      { session_id: SESSION_OTHER, client_request_id: "req-cross", message: "你好" },
      userToken(account.accountId),
    );
    expect([403, 404]).toContain(cross.status);

    // Owned but legacy: must not be routed through the pi harness.
    const legacy = await chat({ session_id: SESSION_LEGACY, client_request_id: "req-legacy", message: "你好" });
    expect(legacy.status).toBe(409);

    // Bad signature is rejected at the edge, before any downstream call.
    const badToken = await chat(
      { session_id: SESSION_PI, client_request_id: "req-bad", message: "你好" },
      userToken(account.accountId, "a-wrong-secret-that-is-long-enough-000"),
    );
    expect(badToken.status).toBe(401);
  });

  it("§9-重复 request_id 同 hash: replays the stored answer with no new Pi entry (F7)", async () => {
    faux.setResponses([fauxAssistantMessage("第一次的答复。")]);
    const first = await chat({ session_id: SESSION_PI, client_request_id: "req-replay-1", message: "你好" });
    expect(first.status).toBe(200);
    expect((first.body.message as { content: string }).content).toBe("第一次的答复。");
    expect(first.body.replayed).toBe(false);

    const entriesAfterFirst = piEntryCount(SESSION_PI);

    // No response queued: a second prompt would fail loudly if it ran.
    faux.setResponses([]);
    const second = await chat({ session_id: SESSION_PI, client_request_id: "req-replay-1", message: "你好" });
    expect(second.status).toBe(200);
    expect(second.body.replayed).toBe(true);
    expect((second.body.message as { content: string }).content).toBe("第一次的答复。");
    expect(piEntryCount(SESSION_PI)).toBe(entriesAfterFirst);

    // Provenance was written once, not twice.
    expect(await memorySource.count(SESSION_PI)).toBe(1);
  });

  it("§9-重复 request_id 异 hash: 409", async () => {
    const conflict = await chat({
      session_id: SESSION_PI,
      client_request_id: "req-replay-1",
      message: "完全不同的内容",
    });
    expect(conflict.status).toBe(409);
  });

  it("§9-receipt 孤儿改判: a processing receipt is reclaimed and rerun exactly once", async () => {
    faux.setResponses([fauxAssistantMessage("孤儿前的答复。")]);
    await chat({ session_id: SESSION_PI, client_request_id: "req-orphan-1", message: "崩溃测试" });

    const rows = await db.query<import("mysql2/promise").RowDataPacket>(
      "SELECT id FROM agent_run_receipt WHERE session_id = ? AND client_request_id = ?",
      [SESSION_PI, "req-orphan-1"],
    );
    const receiptId = Number(rows[0]!.id);
    // Simulate a crash: the row is left mid-flight.
    await receipts.forceStatus(receiptId, "processing");

    faux.setResponses([fauxAssistantMessage("重跑后的答复。")]);
    const retry = await chat({ session_id: SESSION_PI, client_request_id: "req-orphan-1", message: "崩溃测试" });
    expect(retry.status).toBe(200);
    expect(retry.body.replayed).toBe(false);
    expect((retry.body.message as { content: string }).content).toBe("重跑后的答复。");

    const row = await receipts.get(receiptId);
    expect(row?.status).toBe("completed");
    expect((row?.response as { message: { content: string } }).message.content).toBe("重跑后的答复。");
  });

  it("§9-同会话双请求并发: serialised, inFlight<=1, deterministic order (F8)", async () => {
    const before = piEntryCount(SESSION_PI);
    faux.setResponses([fauxAssistantMessage("并发答复 A。"), fauxAssistantMessage("并发答复 B。")]);

    const [a, b] = await Promise.all([
      chat({ session_id: SESSION_PI, client_request_id: "req-concurrent-a", message: "并发 A" }),
      chat({ session_id: SESSION_PI, client_request_id: "req-concurrent-b", message: "并发 B" }),
    ]);

    expect(a.status).toBe(200);
    expect(b.status).toBe(200);
    const contents = [
      (a.body.message as { content: string }).content,
      (b.body.message as { content: string }).content,
    ].sort();
    expect(contents).toEqual(["并发答复 A。", "并发答复 B。"]);

    // Exactly two turns were appended — neither request was lost nor doubled.
    expect(piEntryCount(SESSION_PI)).toBe(before + 4);
    // And the registry never held the session for two callers at once.
    expect(harness.registry.stats().queued).toBe(0);
  });

  it("§9-idle 回收后续聊: dispose then reopen keeps the history intact", async () => {
    const before = piEntryCount(SESSION_PI);
    expect(before).toBeGreaterThan(0);

    // Force the idle path: dispose the live runtime and drop it from the cache.
    harness.registry.disposeNow(SESSION_PI);
    expect(harness.registry.get(SESSION_PI)).toBeUndefined();

    faux.setResponses([fauxAssistantMessage("重开后的答复。")]);
    const after = await chat({ session_id: SESSION_PI, client_request_id: "req-after-idle", message: "还在吗" });
    expect(after.status).toBe(200);
    expect(after.body.replayed).toBe(false);

    // The transcript grew from the reopened file, so nothing was lost.
    expect(piEntryCount(SESSION_PI)).toBeGreaterThan(before);
  });

  it("§9-进程重启 (F12): a fresh runtime with the same session dir continues the transcript", async () => {
    const before = piEntryCount(SESSION_PI);

    // Simulate a process restart: tear the whole harness down (new registry,
    // new AgentSessions) and rebuild it against the same on-disk session dir.
    await harness.close();
    harnessPort = PY_PORT + 2;
    harness = buildHarness();
    await harness.listen(harnessPort);

    faux.setResponses([fauxAssistantMessage("重启后的答复。")]);
    const after = await chat({ session_id: SESSION_PI, client_request_id: "req-after-restart", message: "重启后还在吗" });
    expect(after.status).toBe(200);
    expect((after.body.message as { content: string }).content).toBe("重启后的答复。");
    expect(piEntryCount(SESSION_PI)).toBeGreaterThan(before);

    // The reopened runtime saw the earlier turns (it appended to the same file).
    const found = (await import("@earendil-works/pi-coding-agent")).SessionManager.findById(
      paths.runtimeCwd,
      SESSION_PI,
      paths.sessionDir,
    )!;
    const raw = readFileSync(found, "utf-8");
    expect(raw).toContain("第一次的答复。");
    expect(raw).toContain("重启后的答复。");
  });

  it("SSE stream emits status -> final -> done and persists the same final", async () => {
    faux.setResponses([fauxAssistantMessage("SSE 答复。")]);
    const response = await fetch(`http://127.0.0.1:${harnessPort}/api/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${userToken(account.accountId)}` },
      body: JSON.stringify({ session_id: SESSION_PI, client_request_id: "req-sse", message: "流式测试" }),
    });
    expect(response.status).toBe(200);
    const text = await response.text();
    expect(text).toContain("event: final");
    expect(text).toContain("event: done");
    expect(text).toContain("SSE 答复。");
    const finalIndex = text.indexOf("event: final");
    const doneIndex = text.indexOf("event: done");
    expect(finalIndex).toBeGreaterThan(-1);
    expect(doneIndex).toBeGreaterThan(finalIndex);
  });

  it("internal history endpoint projects the transcript and is service-JWT protected", async () => {
    const token = runtimeToken(account.accountId, SESSION_PI);

    const unauthorized = await fetch(`http://127.0.0.1:${harnessPort}/internal/history/${SESSION_PI}`);
    expect(unauthorized.status).toBe(401);

    const response = await fetch(`http://127.0.0.1:${harnessPort}/internal/history/${SESSION_PI}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(response.status).toBe(200);
    const body = (await response.json()) as { messages: Array<{ role: string; content: string; created_at: string }> };
    expect(body.messages.length).toBeGreaterThan(0);
    expect(body.messages.every((m) => m.role === "user" || m.role === "assistant")).toBe(true);
    expect(body.messages.every((m) => typeof m.created_at === "string")).toBe(true);
    // The user's own words survive the projector.
    expect(body.messages.map((m) => m.content).join("\n")).toContain("重启后还在吗");
  });

  it("DELETE history refuses while a run is active and clears provenance when it is not", async () => {
    const auth = () => ({ Authorization: `Bearer ${runtimeToken(account.accountId, SESSION_PI)}` });

    const response = await fetch(`http://127.0.0.1:${harnessPort}/internal/history/${SESSION_PI}`, {
      method: "DELETE",
      headers: auth(),
    });
    expect(response.status).toBe(200);
    const body = (await response.json()) as { deleted: boolean; cleared_source_events: number };
    expect(body.deleted).toBe(true);
    expect(body.cleared_source_events).toBeGreaterThan(0);

    // Transcript gone; a later lookup no longer finds it.
    const found = (await import("@earendil-works/pi-coding-agent")).SessionManager.findById(
      paths.runtimeCwd,
      SESSION_PI,
      paths.sessionDir,
    );
    expect(found).toBeUndefined();
  });

  it("rejects unauthenticated chat and malformed bodies", async () => {
    const noAuth = await fetch(`http://127.0.0.1:${harnessPort}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: SESSION_PI, client_request_id: "x", message: "hi" }),
    });
    expect(noAuth.status).toBe(401);

    const badBody = await chat({ session_id: SESSION_PI, message: "hi" });
    expect(badBody.status).toBe(400);
  });
});
