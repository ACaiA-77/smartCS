/**
 * Phase 7 acceptance: the harness side of the unified entry.
 *
 * From Phase 7 the browser talks to the Python Business Runtime only; the
 * runtime forwards a pi session's turns here carrying (a) the caller's own
 * user JWT and (b) a service signature naming the runtime. These cases pin the
 * two edge behaviours that makes possible:
 *
 *   * the JSON response carries the observation-only `intent_label`, which the
 *     runtime surfaces as `intent` on its ChatResponse;
 *   * a forwarded turn's service signature, when present, must verify — while
 *     the Phase 1-6 direct-edge contract (user JWT only) keeps working.
 *
 * Real MySQL, real Python internal_api subprocess, real pi runtime, Faux model.
 */

import { existsSync } from "node:fs";
import { mkdirSync, rmSync } from "node:fs";
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

process.env.INTERNAL_SERVICE_JWT_SECRET =
  process.env.INTERNAL_SERVICE_JWT_SECRET ?? "phase7-unified-service-secret-0123456789";

const PY_PORT = 9_160;
const HARNESS_PORT = 9_161;
const SESSION_PI = "phase7-unified-pi-session";
const SESSION_SIG = "phase7-unified-signature-session";

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

function userToken(accountId: number, secret = userJwtSecret(), ttl = 1800): string {
  const now = Math.floor(Date.now() / 1000);
  return signHs256(
    { sub: String(accountId), iat: now, exp: now + ttl, iss: "smartcs", jti: `jti-${now}-${Math.random()}` },
    secret,
  );
}

/** Exactly what internal_api.harness_client.mint_service_token mints. */
function runtimeServiceToken(accountId: number, sessionId: string, clientRequestId: string): string {
  const now = Math.floor(Date.now() / 1000);
  return signHs256(
    {
      iss: "smartcs-business-runtime",
      aud: "smartcs-pi-harness",
      account_id: accountId,
      business_user_id: account.businessUserId,
      session_id: sessionId,
      client_request_id: clientRequestId,
      iat: now,
      exp: now + 60,
    },
    process.env.INTERNAL_SERVICE_JWT_SECRET!,
  );
}

async function chat(
  body: Record<string, unknown>,
  options: { user?: string; serviceToken?: string | null } = {},
): Promise<{ status: number; body: any }> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Authorization: `Bearer ${options.user ?? userToken(account.accountId)}`,
  };
  if (options.serviceToken !== null) {
    headers["X-SmartCS-Service-Token"] =
      `Bearer ${options.serviceToken ?? runtimeServiceToken(account.accountId, String(body.session_id), String(body.client_request_id))}`;
  }
  const response = await fetch(`http://127.0.0.1:${HARNESS_PORT}/api/chat`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  return { status: response.status, body: await response.json() };
}

function piEntryCount(sessionId: string): number {
  const found = SessionManager.findById(paths.runtimeCwd, sessionId, paths.sessionDir);
  if (!found || !existsSync(found)) return 0;
  return SessionManager.open(found, paths.sessionDir, paths.runtimeCwd).getEntries().length;
}

beforeAll(async () => {
  await resetTestDatabase();
  db = testDatabase();
  receipts = new ReceiptStore(db);
  memorySource = new MemorySourceStore(db);

  account = await seedAccount(db, { username: "p7-owner", businessUserId: "bu-p7-owner" });
  await seedSession(db, { sessionId: SESSION_PI, accountId: account.accountId, harnessVersion: "pi" });
  await seedSession(db, { sessionId: SESSION_SIG, accountId: account.accountId, harnessVersion: "pi" });

  python = await startPythonService({ port: PY_PORT });
  pythonClient = new PythonInternalClient({ baseUrl: python.url });

  root = makeTmpDir("phase7-unified-");
  paths = resolveSmartCsPaths({
    runtimeCwd: join(root, "cwd"),
    sessionDir: join(root, "sessions"),
    agentDir: join(root, "agent"),
  });
  mkdirSync(paths.runtimeCwd, { recursive: true });
  mkdirSync(paths.sessionDir, { recursive: true });

  faux = createFauxProvider();
  const registry = new SessionRegistry(async (sessionId) => {
    const handle = await openOrCreatePiSession(paths, sessionId, { provider: "faux", faux });
    return { session: handle.session, origin: handle.origin };
  });
  harness = createHarnessServer({
    paths,
    registry,
    receipts,
    memorySource,
    pythonClient,
    idleEvictionMs: 15 * 60_000,
  });
  await harness.listen(HARNESS_PORT);
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

describe("Phase 7 unified entry (§2)", () => {
  it("a forwarded turn is served and returns the observation label", async () => {
    faux.setResponses([fauxAssistantMessage("已收到您的退款申请。")]);

    const forwarded = await chat({
      session_id: SESSION_PI,
      client_request_id: "p7-forwarded-1",
      message: "我要退款",
    });

    expect(forwarded.status).toBe(200);
    expect(forwarded.body.message.content).toBe("已收到您的退款申请。");
    expect(forwarded.body.replayed).toBe(false);
    expect(forwarded.body.session_id).toBe(SESSION_PI);
    expect(forwarded.body.client_request_id).toBe("p7-forwarded-1");
    // Classification is observation-only, but the unified entry surfaces it.
    expect(forwarded.body.intent_label).toBe("refund");
  });

  it("a tampered runtime signature is refused before the turn runs", async () => {
    const before = piEntryCount(SESSION_SIG);
    const tampered =
      runtimeServiceToken(account.accountId, SESSION_SIG, "p7-sig-1").slice(0, -2) + "xx";

    const refused = await chat(
      { session_id: SESSION_SIG, client_request_id: "p7-sig-1", message: "我要退款" },
      { serviceToken: tampered },
    );

    expect(refused.status).toBe(401);
    expect(piEntryCount(SESSION_SIG)).toBe(before);
    // Provenance is written before any model work: no row means no turn ran.
    expect(await receipts.sourceEventId(SESSION_SIG, "p7-sig-1")).toBeUndefined();
  });

  it("a malformed signature header is refused too", async () => {
    const refused = await chat(
      { session_id: SESSION_SIG, client_request_id: "p7-sig-2", message: "你好" },
      { serviceToken: "not-a-jwt" },
    );
    expect(refused.status).toBe(401);
  });

  it("the direct edge contract (user JWT only) is unchanged", async () => {
    faux.setResponses([fauxAssistantMessage("不带头部的直连回答。")]);

    const direct = await chat(
      { session_id: SESSION_SIG, client_request_id: "p7-direct-1", message: "查订单" },
      { serviceToken: null },
    );

    expect(direct.status).toBe(200);
    expect(direct.body.message.content).toBe("不带头部的直连回答。");
    // "查订单" hits the order rule in classifyIntent.
    expect(direct.body.intent_label).toBe("order");
  });
});
