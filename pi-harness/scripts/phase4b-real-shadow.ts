/**
 * Phase 4B — real-model shadow run (HANDOFF-phase4b.md).
 *
 * Runs the SAME 14 scenarios from tests/fixtures/phase4-scenarios.json through
 * the real Moonshot/kimi endpoint in shadow mode, and OBSERVES what the model
 * actually decided. Nothing is scripted: the per-scenario `pi_scripted_calls`
 * field is deliberately ignored — those exist only for the Phase 4 comparison.
 *
 * Hard rules carried over:
 *   * zero real WRITE (shadow mode; the canned interception path is unchanged);
 *   * budget ceiling — stop immediately once total tokens exceed the cap;
 *   * no tuning of scenarios or prompts to make the model look good.
 *
 * Usage: npx tsx scripts/phase4b-real-shadow.ts
 * Output: one JSON document on stdout, plus a line-by-line progress log on stderr.
 */

// Must be set before the harness OR the Python subprocess starts: the internal
// channel authenticates with this shared secret and fails closed without it.
process.env.INTERNAL_SERVICE_JWT_SECRET =
  process.env.INTERNAL_SERVICE_JWT_SECRET ?? "phase4b-service-secret-0123456789abcdef";

import { mkdirSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { SessionManager } from "@earendil-works/pi-coding-agent";
import { SessionRegistry } from "../src/session/registry.js";
import { openOrCreatePiSession } from "../src/session/pi-session.js";
import { PythonInternalClient } from "../src/business/python-client.js";
import { ReceiptStore } from "../src/db/receipts.js";
import { MemorySourceStore } from "../src/db/memory-source.js";
import { ShadowPlanStore } from "../src/db/shadow-plans.js";
import { createHarnessServer } from "../src/server/app.js";
import { signHs256 } from "../src/business/jwt-hs256.js";
import { PI_HARNESS_ROOT, resolveSmartCsPaths, userJwtSecret, type SmartCsPaths } from "../src/config/env.js";
import { makeTmpDir } from "../tests/helpers/harness.js";
import {
  resetTestDatabase,
  seedAccount,
  seedSession,
  startPythonService,
  testDatabase,
} from "../tests/helpers/phase1.js";

const PY_PORT = 9_010;
const HARNESS_PORT = 9_011;
// Fresh per run: reusing session ids would append to the previous run's
// transcript and silently turn a one-shot measurement into a multi-turn one.
const RUN_STAMP = Date.now().toString(36);
const SESSION_ROOT = `phase4b-${RUN_STAMP}`;
const USER_ID = "user_002";
const TOKEN_BUDGET = 500_000;
/** A scenario fails hard after this many consecutive transport errors. */
const MAX_SCENARIO_RETRIES = 2;

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

const WRITE_TOOLS = new Set(["refund_confirm", "ticket_create"]);

function log(line: string): void {
  process.stderr.write(`${line}\n`);
}

async function main(): Promise<void> {
  const fixture = JSON.parse(
    readFileSync(join(PI_HARNESS_ROOT, "tests", "fixtures", "phase4-scenarios.json"), "utf-8"),
  ) as { scenarios: Scenario[] };
  const scenarios = fixture.scenarios;

  await resetTestDatabase();
  const db = testDatabase();
  const receipts = new ReceiptStore(db);
  const memorySource = new MemorySourceStore(db);
  const plans = new ShadowPlanStore(db);
  await plans.ensureTable();

  const account = await seedAccount(db, { username: "phase4b-owner", businessUserId: USER_ID });
  await seedSession(db, {
    sessionId: `${SESSION_ROOT}-warm`,
    accountId: account.accountId,
    harnessVersion: "pi",
  });

  const root = makeTmpDir("phase4b-");
  const paths: SmartCsPaths = resolveSmartCsPaths({
    runtimeCwd: join(root, "cwd"),
    sessionDir: join(root, "sessions"),
    agentDir: join(root, "agent"),
  });
  mkdirSync(paths.runtimeCwd, { recursive: true });
  mkdirSync(paths.sessionDir, { recursive: true });

  const python = await startPythonService({ port: PY_PORT, orderDbPath: join(root, "orders.db") });
  // Record every internal request so the "zero write call" claim is asserted at
  // the network layer in THIS round too, not just inherited from Phase 4.
  const internalRequests: Array<{ url: string; body: string }> = [];
  const recordingFetch: typeof fetch = (input, init) => {
    internalRequests.push({ url: String(input), body: String(init?.body ?? "") });
    return fetch(input as never, init);
  };
  const internal = new PythonInternalClient({ baseUrl: python.url, fetchImpl: recordingFetch });

  // REAL provider: resolveProviderMode() reads the repository-root .env -> openai.
  const registry = new SessionRegistry(async (sessionId) => {
    const handle = await openOrCreatePiSession(paths, sessionId, {
      provider: "openai",
      toolMode: "business",
      pythonClient: internal,
      writeMode: "shadow",
      shadowPlanStore: plans,
    });
    return {
      session: handle.session,
      origin: handle.origin,
      turnContext: handle.turnContext,
      snapshotHolder: handle.snapshotHolder,
    };
  });

  const harness = createHarnessServer({
    paths,
    registry,
    receipts,
    memorySource,
    pythonClient: internal,
    idleEvictionMs: 30 * 60_000,
  });
  await harness.listen(HARNESS_PORT);

  const now = Math.floor(Date.now() / 1000);
  // The edge enforces USER_JWT_MAX_TTL_SECONDS (1800); a longer token is
  // rejected outright, which is exactly what a 1-hour token produced.
  const token = signHs256(
    { sub: String(account.accountId), iat: now, exp: now + 1800, iss: "smartcs", jti: `p4b-${now}` },
    userJwtSecret(),
  );

  let totalTokens = 0;
  let cachedTokens = 0;
  let authFailures = 0;
  const results: Array<Record<string, unknown>> = [];
  let aborted: string | undefined;

  for (const scenario of scenarios) {
    if (authFailures >= 3) {
      aborted = `aborting after ${authFailures} consecutive authentication failures`;
      break;
    }
    if (totalTokens >= TOKEN_BUDGET) {
      aborted = `token budget exhausted before ${scenario.id}`;
      break;
    }
    const sessionId = `${SESSION_ROOT}-${scenario.id}`;
    await db.execute(
      `INSERT INTO conversation_session (session_id, account_id, title, harness_version)
       VALUES (?, ?, '', 'pi') ON DUPLICATE KEY UPDATE session_id = session_id`,
      [sessionId, account.accountId],
    );

    const turns: Array<Record<string, unknown>> = [];
    let scenarioError: string | undefined;

    for (const [index, message] of scenario.turns.entries()) {
      let attempt = 0;
      for (;;) {
        try {
          const response = await fetch(`http://127.0.0.1:${HARNESS_PORT}/api/chat`, {
            method: "POST",
            headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
            body: JSON.stringify({
              session_id: sessionId,
              client_request_id: `${scenario.id}-t${index}`,
              message,
            }),
          });
          const body = (await response.json()) as Record<string, unknown>;
          if (response.status !== 200) throw new Error(`HTTP ${response.status}: ${JSON.stringify(body)}`);
          turns.push({
            index,
            user: message,
            answer: String((body.message as { content?: string } | undefined)?.content ?? "").slice(0, 400),
          });
          break;
        } catch (error) {
          // A transport failure is not the model's decision — retry within
          // budget, then record the scenario as errored rather than guessing.
          attempt += 1;
          if (attempt > MAX_SCENARIO_RETRIES) {
            scenarioError = type(error);
            if (scenarioError.includes("401")) authFailures += 1;
            break;
          }
          log(`  retry ${attempt}/${MAX_SCENARIO_RETRIES} for ${scenario.id} turn ${index}`);
        }
      }
      if (scenarioError) break;
    }

    // Whatever happened, read back what the model ACTUALLY did.
    const usage = measureSession(paths, sessionId);
    totalTokens += usage.total;
    cachedTokens += usage.cached;

    const calls = toolCalls(paths, sessionId);
    const writeCalls = calls.filter((call) => WRITE_TOOLS.has(call.name));
    const confirmTurnIndexes = turns
      .map((turn, index) => ({ index, calls: calls.filter((c) => c.turn === turn.index) }))
      .filter((entry) => entry.calls.some((c) => c.name === "refund_confirm"))
      .map((entry) => entry.index);

    const planRows = (await plans.listFor(sessionId)).map((row) => ({
      tool: row.toolName,
      args: row.arguments,
      turn: row.turnIndex,
    }));

    results.push({
      id: scenario.id,
      category: scenario.category,
      expect_no_write: scenario.expect_no_write,
      adversarial: scenario.adversarial === true,
      error: scenarioError,
      turns,
      calls: calls.map((call) => ({ turn: call.turn, name: call.name, arguments: call.arguments })),
      write_calls: writeCalls.map((call) => ({ turn: call.turn, name: call.name })),
      confirm_turn_indexes: confirmTurnIndexes,
      plan_rows: planRows,
      tokens: usage.total,
    });

    log(
      `[4B] ${scenario.id}: calls=${calls.map((c) => c.name).join(",") || "none"} ` +
        `writes=${writeCalls.map((c) => c.name).join(",") || "none"} tokens=${usage.total} total=${totalTokens}`,
    );
    if (scenarioError) log(`[4B] ${scenario.id} ERROR: ${scenarioError}`);
  }

  // Parse the body: a substring match would flag session ids and client request
  // ids (scenario ids literally contain "ticket_create"), which is not what the
  // assertion is about. Only a request that actually asked to EXECUTE a write
  // tool counts.
  const executedWriteTools: string[] = [];
  for (const request of internalRequests) {
    if (!request.url.includes("/internal/tools/execute")) continue;
    try {
      const parsed = JSON.parse(request.body) as { tool?: unknown };
      if (parsed.tool === "refund_confirm" || parsed.tool === "ticket_create") {
        executedWriteTools.push(String(parsed.tool));
      }
    } catch {
      /* non-JSON body on a tool endpoint would itself be a bug; ignore here */
    }
  }
  log(
    `[4B] total tokens=${totalTokens} (cacheRead=${cachedTokens}) budget=${TOKEN_BUDGET} ` +
      `internalRequests=${internalRequests.length} executedWriteTools=${executedWriteTools.length}`,
  );

  process.stdout.write(
    `${JSON.stringify(
      {
        scenarios: results,
        totalTokens,
        cachedTokens,
        budget: TOKEN_BUDGET,
        aborted,
        internalRequests: internalRequests.length,
        executedWriteTools,
        toolExecuteRequests: internalRequests.filter((r) => r.url.includes("/internal/tools/execute")).length,
        runStamp: RUN_STAMP,
      },
      null,
      2,
    )}\n`,
  );

  await harness.close();
  await python.stop();
  await db.close();
  try {
    rmSync(root, { recursive: true, force: true });
  } catch {
    /* disposable */
  }
}

function type(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** Every tool call the model made, tagged with the turn it happened in. */
function toolCalls(paths: SmartCsPaths, sessionId: string): Array<{ turn: number; name: string; arguments: Record<string, unknown> }> {
  const found = SessionManager.findById(paths.runtimeCwd, sessionId, paths.sessionDir);
  if (!found) return [];
  const manager = SessionManager.open(found, paths.sessionDir, paths.runtimeCwd);
  const out: Array<{ turn: number; name: string; arguments: Record<string, unknown> }> = [];
  let turn = -1;
  for (const entry of manager.getEntries()) {
    if (entry.type !== "message") continue;
    const message = (entry as { message?: { role?: string; content?: unknown } }).message;
    if (message?.role === "user") {
      const text = typeof message.content === "string" ? message.content : "";
      // Only count real user turns; the injected snapshot is a custom message.
      if (!text.startsWith("[SmartCS 业务上下文快照")) turn += 1;
      continue;
    }
    if (message?.role !== "assistant" || !Array.isArray(message.content)) continue;
    for (const block of message.content as Array<{ type?: string; name?: string; arguments?: unknown }>) {
      if (block?.type === "toolCall" && typeof block.name === "string") {
        out.push({
          turn,
          name: block.name,
          arguments: (block.arguments ?? {}) as Record<string, unknown>,
        });
      }
    }
  }
  return out;
}

/** Real token consumption, summed from provider usage on assistant messages. */
function measureSession(paths: SmartCsPaths, sessionId: string): { total: number; cached: number } {
  const found = SessionManager.findById(paths.runtimeCwd, sessionId, paths.sessionDir);
  if (!found) return { total: 0, cached: 0 };
  const manager = SessionManager.open(found, paths.sessionDir, paths.runtimeCwd);
  let total = 0;
  let cached = 0;
  for (const entry of manager.getEntries()) {
    if (entry.type !== "message") continue;
    const message = (entry as { message?: { role?: string; usage?: Record<string, number> } }).message;
    if (message?.role !== "assistant" || !message.usage) continue;
    const usage = message.usage;
    total +=
      (usage.input ?? 0) + (usage.output ?? 0) + (usage.cacheRead ?? 0) + (usage.cacheWrite ?? 0);
    cached += usage.cacheRead ?? 0;
  }
  return { total, cached };
}

main().catch((error) => {
  process.stderr.write(`[4B] fatal: ${type(error)}\n${(error as Error)?.stack ?? ""}\n`);
  process.exit(1);
});
