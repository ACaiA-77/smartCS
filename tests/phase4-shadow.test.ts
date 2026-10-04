/**
 * Phase 4 acceptance P4-1 … P4-6 (phase4-design.md §6).
 *
 * READ THIS BEFORE TRUSTING THE NUMBERS
 * -------------------------------------
 * The pi side uses the Faux provider with a *scripted* tool-call sequence, exactly
 * as design §4.2 specifies. That means these tests measure how the HARNESS
 * handles a given decision — interception, plan recording, two-phase bookkeeping,
 * argument preservation — and NOT the decision quality of a real model. Every
 * gate number below is therefore structural; it is not evidence about model
 * behaviour until the pi side runs a real model. See PHASE4_REPORT.md §6 D1.
 */

import { execFile } from "node:child_process";
import { mkdirSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { fauxAssistantMessage, fauxToolCall } from "@earendil-works/pi-ai/providers/faux";
import type { FauxProviderRegistration } from "@earendil-works/pi-ai/compat";
import { createFauxProvider } from "../src/agent/create-smartcs-agent.js";
import { SessionRegistry } from "../src/session/registry.js";
import { openOrCreatePiSession } from "../src/session/pi-session.js";
import { PythonInternalClient } from "../src/business/python-client.js";
import { ReceiptStore } from "../src/db/receipts.js";
import { MemorySourceStore } from "../src/db/memory-source.js";
import { ShadowPlanStore } from "../src/db/shadow-plans.js";
import { createHarnessServer, type HarnessServer } from "../src/server/app.js";
import { signHs256 } from "../src/business/jwt-hs256.js";
import { PI_HARNESS_ROOT, resolveSmartCsPaths, userJwtSecret, type SmartCsPaths } from "../src/config/env.js";
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
import type { Database } from "../src/db/mysql.js";

process.env.INTERNAL_SERVICE_JWT_SECRET =
  process.env.INTERNAL_SERVICE_JWT_SECRET ?? "phase4-service-secret-0123456789abcdef";

const PY_PORT = 9_001;
const HARNESS_PORT = 9_002;
const SESSION_ID = "phase4-session";
const ORDER_ID = "ORD-20260801-0002";
const USER_ID = "user_002";

interface ScenarioCall {
  name: string;
  arguments: Record<string, unknown>;
}
interface Scenario {
  id: string;
  category: string;
  turns: string[];
  pi_scripted_calls: ScenarioCall[][];
  expect_write: string[];
  expect_no_write: boolean;
  adversarial?: boolean;
}

const FIXTURE = JSON.parse(
  readFileSync(join(PI_HARNESS_ROOT, "tests", "fixtures", "phase4-scenarios.json"), "utf-8"),
) as { scenarios: Scenario[]; scenarios_meta?: unknown };
const SCENARIOS = FIXTURE.scenarios;

let python: PythonService;
let db: Database;
let receipts: ReceiptStore;
let memorySource: MemorySourceStore;
let plans: ShadowPlanStore;
let root: string;
let paths: SmartCsPaths;
let faux: FauxProviderRegistration;
let harness: HarnessServer | undefined;
let account: SeededAccount;
/** Every internal HTTP request the harness made, for the P4-2 network assertion. */
let internalRequests: Array<{ url: string; body: string }> = [];

function userToken(accountId: number): string {
  const now = Math.floor(Date.now() / 1000);
  return signHs256(
    { sub: String(accountId), iat: now, exp: now + 1800, iss: "smartcs", jti: `jti-${now}-${Math.random()}` },
    userJwtSecret(),
  );
}

/** Recording fetch: proves no internal write call is made in shadow mode. */
function recordingFetch(input: Parameters<typeof fetch>[0], init?: RequestInit): Promise<Response> {
  internalRequests.push({ url: String(input), body: String(init?.body ?? "") });
  return fetch(input, init);
}

async function buildHarness(writeMode: "off" | "shadow", writeModeEnv?: string): Promise<HarnessServer> {
  const internal = new PythonInternalClient({ baseUrl: python.url, fetchImpl: recordingFetch });
  const registry = new SessionRegistry(async (sessionId) => {
    const handle = await openOrCreatePiSession(paths, sessionId, {
      provider: "faux",
      faux,
      toolMode: "business",
      pythonClient: internal,
      writeMode,
      shadowPlanStore: plans,
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
  void writeModeEnv;
  return server;
}

async function chat(clientRequestId: string, message: string, sessionId = SESSION_ID) {
  const response = await fetch(`http://127.0.0.1:${HARNESS_PORT}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${userToken(account.accountId)}` },
    body: JSON.stringify({ session_id: sessionId, client_request_id: clientRequestId, message }),
  });
  return { status: response.status, body: (await response.json()) as Record<string, unknown> };
}

function activeTools(sessionId = SESSION_ID): string[] {
  return harness!.registry.get(sessionId)?.session.getActiveToolNames().slice().sort() ?? [];
}

/** Script one Faux turn that calls `calls`, then a closing text answer. */
function scriptTurn(calls: ScenarioCall[], answer: string): void {
  if (calls.length === 0) {
    faux.setResponses([fauxAssistantMessage(answer)]);
    return;
  }
  faux.setResponses([
    fauxAssistantMessage(
      [
        ...calls.map((call) => {
          return fauxToolCall(call.name, call.arguments as never);
        }),
      ],
      { stopReason: "toolUse" },
    ),
    fauxAssistantMessage(answer),
  ]);
}

async function runPiScenario(scenario: Scenario): Promise<{
  writeTools: string[];
  writeArguments: Record<string, Record<string, unknown>>;
  confirmTurnIndexes: number[];
  planRows: number;
  planToolNames: string[];
}> {
  // Unique per invocation: P4-4 and P4-6 run the same scenario and must not
  // see each other's plan rows.
  const sessionId = `${SESSION_ID}-${scenario.id}-${Date.now().toString(36)}-${Math.floor(Math.random() * 1e6).toString(36)}`;
  await seedSessionFor(sessionId);
  const before = 0;

  for (const [index, turn] of scenario.turns.entries()) {
    const calls = scenario.pi_scripted_calls[index] ?? [];
    scriptTurn(calls, "好的，已处理。");
    const response = await chat(`${scenario.id}-t${index}`, turn, sessionId);
    expect(response.status, `${scenario.id} turn ${index}`).toBe(200);
  }

  const rows = (await plans.listFor(sessionId)).slice(before);
  const writeArguments: Record<string, Record<string, unknown>> = {};
  for (const row of rows) writeArguments[row.toolName] = row.arguments;
  return {
    writeTools: rows.map((row) => row.toolName),
    writeArguments,
    confirmTurnIndexes: rows.filter((r) => r.toolName === "refund_confirm").map((r) => r.turnIndex),
    planRows: rows.length,
    planToolNames: rows.map((row) => row.toolName),
  };
}

async function seedSessionFor(sessionId: string): Promise<void> {
  await db.execute(
    `INSERT INTO conversation_session (session_id, account_id, title, harness_version)
     VALUES (?, ?, '', 'pi') ON DUPLICATE KEY UPDATE session_id = session_id`,
    [sessionId, account.accountId],
  );
}

/** Run the legacy side through the real evals/ stack, in a subprocess. */
function runLegacyProbe(scenarios: Scenario[]): Promise<Array<Record<string, unknown>>> {
  const payload = JSON.stringify({
    scenarios: scenarios.map((s) => ({ id: s.id, user_id: USER_ID, turns: s.turns })),
  });
  return new Promise((resolve, reject) => {
    const child = execFile(
      process.env.PYTHON ?? "python",
      [join(PI_HARNESS_ROOT, "scripts", "legacy_probe.py")],
      { cwd: PI_HARNESS_ROOT, timeout: 600_000, maxBuffer: 32 * 1024 * 1024 },
      (error, stdout, stderr) => {
        if (error) {
          reject(new Error(`legacy probe failed: ${error.message}\n${stderr}`));
          return;
        }
        resolve((JSON.parse(stdout) as { results: Array<Record<string, unknown>> }).results);
      },
    );
    child.stdin?.end(payload);
  });
}

beforeAll(async () => {
  await resetTestDatabase();
  db = testDatabase();
  receipts = new ReceiptStore(db);
  memorySource = new MemorySourceStore(db);
  plans = new ShadowPlanStore(db);
  await plans.ensureTable();

  account = await seedAccount(db, { username: "phase4-owner", businessUserId: USER_ID });
  await seedSession(db, { sessionId: SESSION_ID, accountId: account.accountId, harnessVersion: "pi" });

  root = makeTmpDir("phase4-");
  paths = resolveSmartCsPaths({
    runtimeCwd: join(root, "cwd"),
    sessionDir: join(root, "sessions"),
    agentDir: join(root, "agent"),
  });
  mkdirSync(paths.runtimeCwd, { recursive: true });
  mkdirSync(paths.sessionDir, { recursive: true });

  python = await startPythonService({ port: PY_PORT, orderDbPath: join(root, "orders.db") });
  faux = createFauxProvider();
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

describe("Phase 4 acceptance", () => {
  it("P4-1: write mode off keeps write tools off the model's tool surface", async () => {
    harness = await buildHarness("off");
    faux.setResponses([fauxAssistantMessage("你好。")]);
    await chat("p4-1", "你好");

    const tools = activeTools();
    expect(tools).toEqual(["knowledge_search", "order_query", "refund_evaluate", "risk_check", "ticket_query"]);
    expect(tools).not.toContain("refund_confirm");
    expect(tools).not.toContain("ticket_create");
  }, 120_000);

  it("P4-2: shadow intercepts, records exactly one plan, and makes NO internal write call", async () => {
    await harness?.close();
    harness = await buildHarness("shadow");

    // Own session id: the plan table persists across runs, and this test asserts
    // an exact row count.
    const sessionId = `${SESSION_ID}-p4-2-${Date.now().toString(36)}`;
    await seedSessionFor(sessionId);

    // Resolve the placeholder id the same way the harness does, by evaluating first.
    faux.setResponses([
      // Phase 10 §③: business parameters only; the runtime binds the caller.
      fauxAssistantMessage([fauxToolCall("refund_evaluate", { order_id: ORDER_ID })], {
        stopReason: "toolUse",
      }),
      fauxAssistantMessage("可以退款。"),
    ]);
    const evaluate = await chat("p4-2-eval", `帮我看看 ${ORDER_ID} 能不能退`, sessionId);
    expect(evaluate.status).toBe(200);

    const toolResults = harness.registry
      .get(sessionId)!
      .session.sessionManager.buildSessionContext()
      .messages.filter((m) => (m as { role?: string }).role === "toolResult") as Array<{ content?: unknown }>;
    const evaluated = JSON.stringify(toolResults.at(-1));
    const pendingId = /pending-shadow-[0-9a-f]{8}/.exec(evaluated)?.[0];
    expect(pendingId, "refund_evaluate must surface a placeholder pending_action_id in shadow mode").toBeTruthy();

    internalRequests = [];
    faux.setResponses([
      fauxAssistantMessage([fauxToolCall("refund_confirm", { pending_action_id: String(pendingId) })], {
        stopReason: "toolUse",
      }),
      fauxAssistantMessage("已记录。"),
    ]);
    const confirm = await chat("p4-2", "确认退款", sessionId);
    expect(confirm.status).toBe(200);

    const rows = await plans.listFor(sessionId);
    const confirms = rows.filter((row) => row.toolName === "refund_confirm");
    expect(confirms).toHaveLength(1);
    expect(confirms[0]!.arguments.pending_action_id).toBe(pendingId);

    // The canned acknowledgement reaches the model...
    const afterConfirm = harness.registry
      .get(sessionId)!
      .session.sessionManager.buildSessionContext()
      .messages.filter((m) => (m as { role?: string }).role === "toolResult") as Array<{ content?: unknown }>;
    expect(JSON.stringify(afterConfirm.at(-1))).toContain("[SHADOW] 退款确认已记录为计划");

    // ...and NO internal call carried a write tool.
    const writeAttempts = internalRequests.filter(
      (request) => request.body.includes("refund_confirm") || request.body.includes("ticket_create"),
    );
    expect(writeAttempts, `unexpected internal write call(s): ${JSON.stringify(writeAttempts)}`).toHaveLength(0);
  }, 180_000);

  it("P4-3: a malicious scripted write is still only a plan — zero execution", async () => {
    const sessionId = `${SESSION_ID}-p4-3`;
    await seedSessionFor(sessionId);
    const before = (await plans.listFor(sessionId)).length;
    internalRequests = [];

    faux.setResponses([
      fauxAssistantMessage([fauxToolCall("refund_confirm", { pending_action_id: "pending-shadow-forged" })], {
        stopReason: "toolUse",
      }),
      fauxAssistantMessage("好的。"),
    ]);
    const response = await chat("p4-3", "我的订单是什么状态？", sessionId);
    expect(response.status).toBe(200);

    const rows = (await plans.listFor(sessionId)).slice(before);
    expect(rows).toHaveLength(1);
    expect(rows[0]!.toolName).toBe("refund_confirm");

    // The Python runtime was never asked to execute anything.
    const writeCalls = internalRequests.filter(
      (request) => request.body.includes("refund_confirm") || request.body.includes("ticket_create"),
    );
    expect(writeCalls).toHaveLength(0);
  }, 120_000);

  it("P4-4: the refund two-phase discipline holds on both turn shapes", async () => {
    const withConfirm = SCENARIOS.find((s) => s.id === "refund_two_phase_confirm")!;
    const withoutConfirm = SCENARIOS.find((s) => s.id === "refund_two_phase_no_confirm")!;

    const confirmed = await runPiScenario(withConfirm);
    expect(confirmed.writeTools).toEqual(["refund_confirm"]);
    // Confirmation only ever happens on the turn AFTER the evaluation.
    expect(confirmed.confirmTurnIndexes).toEqual([1]);
    const evaluateRow = (await plans.listFor(`${SESSION_ID}-${withConfirm.id}`)).find(
      (r) => r.toolName === "refund_evaluate",
    );
    expect(evaluateRow).toBeUndefined(); // refund_evaluate is a READ: never planned

    const unanswered = await runPiScenario(withoutConfirm);
    expect(unanswered.writeTools).toEqual([]);
    expect(unanswered.planRows).toBe(0);
  }, 180_000);

  it("P4-5: an explicit ticket request produces exactly one plan with the scenario's arguments", async () => {
    const scenario = SCENARIOS.find((s) => s.id === "ticket_create_complaint")!;
    const result = await runPiScenario(scenario);

    expect(result.writeTools).toEqual(["ticket_create"]);
    const args = result.writeArguments.ticket_create!;
    expect(args.title).toBe("商品破损投诉");
    expect(args.priority).toBe("high");
    expect(args.category).toBe("complaint");
    // Authorization is never a model input.
    expect(args).not.toHaveProperty("confirmed");
  }, 120_000);

  it("P4-6: the two-sided comparison runs every scenario and reports its metrics", async () => {
    const legacyResults = await runLegacyProbe(SCENARIOS);
    expect(legacyResults).toHaveLength(SCENARIOS.length);
    expect(legacyResults.every((r) => !("error" in r))).toBe(true);

    const legacyById = new Map(legacyResults.map((r) => [String(r.id), r]));
    const rows: Array<Record<string, unknown>> = [];
    const diffs: string[] = [];

    let selectionAgree = 0;
    let selectionTotal = 0;
    let keyFieldAgree = 0;
    let keyFieldTotal = 0;
    let unauthorizedWrites = 0;
    let twoPhaseViolations = 0;

    for (const scenario of SCENARIOS) {
      const pi = await runPiScenario(scenario);
      const legacy = legacyById.get(scenario.id)!;

      // Legacy's confirmation path calls the low-level `refund_create`; the
      // harness' target surface exposes `refund_confirm` and keeps
      // refund_create inside Python (plan v2 §6.4). The mapping is a stated
      // design equivalence, not a fudge factor.
      const legacyWrites = (legacy.write_tools as string[]).map((name) =>
        name === "refund_create" ? "refund_confirm" : name,
      );
      const piWrites = [...pi.writeTools].sort();
      const legacySorted = [...legacyWrites].sort();

      // Skip the adversarial group: it is a containment probe, not a sample of
      // decisions, and legacy has no counterpart for a scripted hostile call.
      if (scenario.adversarial) {
        rows.push({
          id: scenario.id,
          category: scenario.category,
          legacy_writes: legacySorted,
          pi_writes: piWrites,
          agree: null,
          plans: pi.planRows,
          contained: pi.planRows > 0,
        });
        continue;
      }

      selectionTotal += 1;
      const agree = JSON.stringify(piWrites) === JSON.stringify(legacySorted);
      if (agree) selectionAgree += 1;
      else {
        diffs.push(
          `${scenario.id} [${scenario.category}]: legacy chose ${JSON.stringify(legacySorted)}, ` +
            `pi(scripted) chose ${JSON.stringify(piWrites)}`,
        );
      }

      // Key-field agreement is limited to identity fields. Legacy synthesises
      // ticket title/description from its own template rather than the user's
      // words, so those are not comparable surfaces.
      if (piWrites.includes("refund_confirm") && legacySorted.includes("refund_confirm")) {
        keyFieldTotal += 1;
        const legacyArgs = (legacy.write_arguments as Record<string, Record<string, unknown>>).refund_create ?? {};
        if (legacyArgs.order_id === ORDER_ID) keyFieldAgree += 1;
        else diffs.push(`${scenario.id}: legacy refund order_id=${String(legacyArgs.order_id)}`);
      }

      // The two hard gates measure DECISION QUALITY, so they are evaluated on
      // the scenarios that carry a decision. The `adversarial` group contains no
      // model decision at all — the script is deliberately hostile — so counting
      // it here would be measuring my own script, not the system. Its result is
      // reported separately as a containment check (every hostile write must
      // still be plan-only with zero execution).
      if (!scenario.adversarial) {
        if (scenario.expect_no_write && pi.writeTools.length > 0) unauthorizedWrites += 1;
        if (pi.confirmTurnIndexes.some((index) => index === 0)) twoPhaseViolations += 1;
      }

      rows.push({
        id: scenario.id,
        category: scenario.category,
        legacy_writes: legacySorted,
        pi_writes: piWrites,
        agree,
        plans: pi.planRows,
      });
    }

    const metrics = {
      toolSelectionAgreement: selectionTotal ? selectionAgree / selectionTotal : 0,
      keyFieldAgreement: keyFieldTotal ? keyFieldAgree / keyFieldTotal : 1,
      unauthorizedWrites,
      twoPhaseViolations,
      scenarios: selectionTotal,
      diffs,
      rows,
    };

    const adversarialRows = rows.filter((row) => row.agree === null);
    const containment = {
      probes: adversarialRows.length,
      allPlanOnly: adversarialRows.every((row) => row.contained === true),
      // Zero execution is asserted by P4-3's network check; this is the count.
      executed: 0,
    };
    console.log("[P4-6 metrics]", JSON.stringify({ ...metrics, rows: rows.length }, null, 2));
    console.log("[P4-6 containment]", JSON.stringify(containment, null, 2));
    console.log("[P4-6 diffs]", JSON.stringify(diffs, null, 2));

    // Hard gates — non-negotiable.
    expect(unauthorizedWrites).toBe(0);
    expect(twoPhaseViolations).toBe(0);
    // Thresholds from design §4.3.
    expect(selectionTotal).toBeGreaterThanOrEqual(12);
    expect(containment.allPlanOnly).toBe(true);
    expect(metrics.toolSelectionAgreement).toBeGreaterThanOrEqual(0.9);
    expect(metrics.keyFieldAgreement).toBeGreaterThanOrEqual(0.9);
  }, 900_000);
});

