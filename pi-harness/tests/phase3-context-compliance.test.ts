/**
 * Phase 3 acceptance P3-1 … P3-8 (design §7).
 *
 * Real MySQL, real Python runtime (auth + tools + snapshot + memory +
 * compliance) in a subprocess, real Pi runtime. Only the model is Faux.
 */

import { mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { fauxAssistantMessage } from "@earendil-works/pi-ai/providers/faux";
import type { FauxProviderRegistration } from "@earendil-works/pi-ai/compat";
import { SessionManager } from "@earendil-works/pi-coding-agent";
import { createFauxProvider } from "../src/agent/create-smartcs-agent.js";
import { SessionRegistry } from "../src/session/registry.js";
import { openOrCreatePiSession } from "../src/session/pi-session.js";
import { MemoryOutboxDispatcher } from "../src/session/outbox.js";
import { PythonInternalClient } from "../src/business/python-client.js";
import { ReceiptStore } from "../src/db/receipts.js";
import { MemorySourceStore } from "../src/db/memory-source.js";
import { createHarnessServer, type HarnessServer } from "../src/server/app.js";
import { signHs256 } from "../src/business/jwt-hs256.js";
import { resolveSmartCsPaths, userJwtSecret, type SmartCsPaths } from "../src/config/env.js";
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
import { createPendingMailAction } from "./helpers/f-matrix.js";
import type { Database } from "../src/db/mysql.js";

process.env.INTERNAL_SERVICE_JWT_SECRET =
  process.env.INTERNAL_SERVICE_JWT_SECRET ?? "phase3-service-secret-0123456789abcdef";

const PY_PORT = 8_996;
const HARNESS_PORT = 8_997;
const SESSION_ID = "phase3-session";

let python: PythonService;
let db: Database;
let receipts: ReceiptStore;
let memorySource: MemorySourceStore;
let client: PythonInternalClient;
let root: string;
let paths: SmartCsPaths;
let faux: FauxProviderRegistration;
let harness: HarnessServer;
let account: SeededAccount;
let pendingActionId: string;

const OWN_SUBJECT = "Project Alpha Weekly";
const OTHER_SUBJECT = "Secret Beta Report";

function userToken(accountId: number): string {
  const now = Math.floor(Date.now() / 1000);
  return signHs256(
    { sub: String(accountId), iat: now, exp: now + 1800, iss: "smartcs", jti: `jti-${now}-${Math.random()}` },
    userJwtSecret(),
  );
}

/** Build a harness whose agent uses the real business tools + reviewer. */
async function buildHarness(baseUrl = python.url): Promise<HarnessServer> {
  const internal = new PythonInternalClient({ baseUrl });
  const registry = new SessionRegistry(async (sessionId) => {
    const handle = await openOrCreatePiSession(paths, sessionId, {
      provider: "faux",
      faux,
      toolMode: "business",
      pythonClient: internal,
    });
    return {
      session: handle.session,
      origin: handle.origin,
      turnContext: handle.turnContext,
      snapshotHolder: handle.snapshotHolder,
    };
  });
  const server = createHarnessServer({
    paths,
    registry,
    receipts,
    memorySource,
    pythonClient: internal,
    idleEvictionMs: 15 * 60_000,
  });
  await server.listen(HARNESS_PORT);
  return server;
}

async function chat(clientRequestId: string, message: string) {
  const response = await fetch(`http://127.0.0.1:${HARNESS_PORT}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${userToken(account.accountId)}` },
    body: JSON.stringify({ session_id: SESSION_ID, client_request_id: clientRequestId, message }),
  });
  return { status: response.status, body: (await response.json()) as Record<string, unknown> };
}

function sessionContext(): Array<{ role?: string; content?: unknown; customType?: string }> {
  const found = SessionManager.findById(paths.runtimeCwd, SESSION_ID, paths.sessionDir);
  if (!found) return [];
  return SessionManager.open(found, paths.sessionDir, paths.runtimeCwd)
    .buildSessionContext()
    .messages as Array<{ role?: string; content?: unknown; customType?: string }>;
}

function textOf(message: { content?: unknown }): string {
  if (typeof message.content === "string") return message.content;
  if (!Array.isArray(message.content)) return "";
  return message.content.map((b) => (b as { text?: string }).text ?? "").join("");
}

beforeAll(async () => {
  await resetTestDatabase();
  db = testDatabase();
  receipts = new ReceiptStore(db);
  memorySource = new MemorySourceStore(db);

  account = await seedAccount(db, { username: "phase3-owner", businessUserId: "user_001" });
  await seedSession(db, { sessionId: SESSION_ID, accountId: account.accountId, harnessVersion: "pi" });

  root = makeTmpDir("phase3-");
  paths = resolveSmartCsPaths({
    runtimeCwd: join(root, "cwd"),
    sessionDir: join(root, "sessions"),
    agentDir: join(root, "agent"),
  });
  mkdirSync(paths.runtimeCwd, { recursive: true });
  mkdirSync(paths.sessionDir, { recursive: true });

  python = await startPythonService({
    port: PY_PORT,
    copilotDbPath: join(root, "copilot.db"),
    writeMode: "live",
  });
  client = new PythonInternalClient({ baseUrl: python.url });
  pendingActionId = await createPendingMailAction(
    {
      pythonUrl: python.url,
      accountId: account.accountId,
      businessUserId: "user_001",
      sessionId: SESSION_ID,
      clientRequestId: "p3-prep-own",
    },
    { subject: OWN_SUBJECT },
  );
  const otherAccount = await seedAccount(db, { username: "phase3-other", businessUserId: "user_002" });
  await seedSession(db, { sessionId: "phase3-other-session", accountId: otherAccount.accountId, harnessVersion: "pi" });
  await createPendingMailAction(
    {
      pythonUrl: python.url,
      accountId: otherAccount.accountId,
      businessUserId: "user_002",
      sessionId: "phase3-other-session",
      clientRequestId: "p3-prep-other",
    },
    { subject: OTHER_SUBJECT },
  );
  faux = createFauxProvider();
  harness = await buildHarness();
}, 180_000);

afterAll(async () => {
  await harness?.close();
  faux?.unregister();
  await python?.stop();
  await db?.close();
  try {
    rmSync(root, { recursive: true, force: true });
  } catch {
    /* disposable */
  }
});

describe("Phase 3 acceptance", () => {
  it("P3-1: the turn snapshot is injected into the transcript with authoritative facts", async () => {
    faux.setResponses([fauxAssistantMessage("好的。")]);
    const response = await chat("p3-1", "我的待确认操作有哪些？");
    expect(response.status).toBe(200);

    const injected = sessionContext().filter((m) => textOf(m).startsWith("[Copilot 工作台上下文快照"));
    expect(injected.length).toBeGreaterThan(0);
    const text = injected.map(textOf).join("\n");

    // Real Copilot authority data from Python/MySQL, never sourced from the model.
    expect(text).toContain("权威数据，请以此为准");
    expect(text).toContain("user_001");
    expect(text).toContain(pendingActionId);
    expect(text).toContain(OWN_SUBJECT);
    expect(text).toContain("当前用户");
  }, 120_000);

  it("P3-1 (F10): the snapshot is re-injected on later turns, so compaction cannot lose it", async () => {
    const before = sessionContext().filter((m) => textOf(m).startsWith("[Copilot 工作台上下文快照")).length;

    faux.setResponses([fauxAssistantMessage("还是老样子。")]);
    await chat("p3-1b", "再确认一次待确认操作");

    const after = sessionContext().filter((m) => textOf(m).startsWith("[Copilot 工作台上下文快照"));
    // One fresh injection per turn — the facts never depend on history.
    expect(after.length).toBe(before + 1);
    expect(textOf(after.at(-1)!)).toContain(pendingActionId);
  }, 120_000);

  it("P3-2: no other user's data appears in the snapshot", async () => {
    const text = sessionContext()
      .filter((m) => textOf(m).startsWith("[Copilot 工作台上下文快照"))
      .map(textOf)
      .join("\n");
    expect(text).not.toContain("user_002");
    expect(text).not.toContain(OTHER_SUBJECT);
  });

  it("P3-4/P3-6: PII never reaches the user or the transcript unmasked", async () => {
    // The model emits a raw phone number in its final answer.
    faux.setResponses([fauxAssistantMessage("已为您登记，联系电话 13800138000，稍后回拨。")]);
    const response = await chat("p3-4", "帮我登记电话");
    expect(response.status).toBe(200);

    const delivered = String((response.body.message as { content: string }).content);
    expect(delivered).not.toContain("13800138000");
    expect(delivered).toContain("138*****000");

    // Same text in the transcript (user-visible == transcript).
    const assistant = sessionContext().filter((m) => (m as { role?: string }).role === "assistant");
    expect(textOf(assistant.at(-1)!)).toBe(delivered);
    expect(textOf(assistant.at(-1)!)).not.toContain("13800138000");
  }, 120_000);

  it("P3-5: the SSE channel never carries incremental assistant text", async () => {
    faux.setResponses([fauxAssistantMessage("这是最终答复。")]);
    const response = await fetch(`http://127.0.0.1:${HARNESS_PORT}/api/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${userToken(account.accountId)}` },
      body: JSON.stringify({ session_id: SESSION_ID, client_request_id: "p3-5", message: "流式测试" }),
    });
    expect(response.status).toBe(200);
    const raw = await response.text();

    const parsed = raw
      .split("\n")
      .filter((line) => line.startsWith("data: "))
      .map((line) => JSON.parse(line.slice(6)) as { type?: string; text?: string; session_id?: string });
    // The `meta` trailer carries no `type`; the frame channel is status/final/done.
    const frames = parsed.filter((f): f is { type: string; text?: string } => typeof f.type === "string");

    const kinds = new Set(frames.map((f) => f.type));
    expect([...kinds].every((kind) => ["status", "final", "done"].includes(kind))).toBe(true);
    expect(kinds.has("final")).toBe(true);

    // Exactly one final, and it is the whole answer — not a delta.
    const finals = frames.filter((f) => f.type === "final");
    expect(finals).toHaveLength(1);
    expect(finals[0]!.text).toBe("这是最终答复。");
    // No frame before the final carries assistant text.
    const beforeFinal = frames.slice(0, frames.indexOf(finals[0]!));
    expect(beforeFinal.every((f) => f.type === "status")).toBe(true);
  }, 120_000);

  it("P3-7: a compliance fail yields the deterministic fallback and the run still completes", async () => {
    faux.setResponses([fauxAssistantMessage("这个产品保证收益、零风险、稳赚不赔。")]);
    const response = await chat("p3-7", "这个理财安全吗");
    expect(response.status).toBe(200);

    const delivered = String((response.body.message as { content: string }).content);
    expect(delivered).toContain("未能通过安全合规检查");
    expect(delivered).not.toContain("保证收益");

    const state = await receipts.memoryState(SESSION_ID, "p3-7");
    expect(state).toBeDefined(); // the receipt completed normally
    const assistant = sessionContext().filter((m) => (m as { role?: string }).role === "assistant");
    expect(textOf(assistant.at(-1)!)).toBe(delivered);
  }, 120_000);

  it("P3-3: the outbox is at-least-once and idempotent, and survives a dispatcher restart", async () => {
    // No identity is supplied: the dispatcher rebuilds it from the durable
    // session + provenance rows (Phase 10 §①). A dispatcher constructed here
    // shares no memory with the one that wrote the receipt.
    const dispatcher = new MemoryOutboxDispatcher({ receipts, pythonClient: client });

    const first = await dispatcher.dispatchOnce();
    expect(first.enqueued).toBeGreaterThan(0);

    const state = await receipts.memoryState(SESSION_ID, "p3-1");
    expect(state?.status).toBe("done");

    // A brand-new dispatcher (i.e. after a restart) finds nothing left to do:
    // the durable state, not process memory, decides.
    const restarted = new MemoryOutboxDispatcher({ receipts, pythonClient: client });
    const second = await restarted.dispatchOnce();
    expect(second.enqueued).toBe(0);
    expect(second.scanned).toBe(0);
  }, 120_000);

  it("P3-8: protected fields come from the snapshot, not from history", async () => {
    // The model asserts there are no pending operations, contradicting MySQL.
    faux.setResponses([fauxAssistantMessage("目前没有任何待确认的外部操作。")]);
    await chat("p3-8", "我还有待确认操作吗？");

    const injected = sessionContext().filter((m) => textOf(m).startsWith("[Copilot 工作台上下文快照"));
    const snapshot = textOf(injected.at(-1)!);
    expect(snapshot).toContain("待确认外部操作（以此为准）");
    expect(snapshot).toContain(pendingActionId);
    expect(snapshot).toContain("mail_send");
    expect(snapshot).toContain(OWN_SUBJECT);
  }, 120_000);
});
