/**
 * Phase 6 acceptance (phase6-design.md §6): P6-1, P6-3, P6-4, P6-5.
 *
 * Real MySQL, real Python internal_api subprocess (with the Phase 6 audit
 * endpoint and the in-process trace recorder), real Pi runtime with the Faux
 * provider, real harness HTTP edge. The only double is the model itself.
 *
 * P6-2 (audit idempotency) is asserted from the Python side in
 * `python-impl/tests/test_internal_api_audit.py`, where the row counts live.
 */

import { mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { fauxAssistantMessage, fauxToolCall } from "@earendil-works/pi-ai/providers/faux";
import type { RowDataPacket } from "mysql2/promise";
import type { FauxProviderRegistration } from "@earendil-works/pi-ai/compat";
import { createFauxProvider } from "../src/agent/create-smartcs-agent.js";
import { SessionRegistry } from "../src/session/registry.js";
import { openOrCreatePiSession } from "../src/session/pi-session.js";
import { PythonInternalClient, mintServiceToken } from "../src/business/python-client.js";
import { ReceiptStore } from "../src/db/receipts.js";
import { MemorySourceStore } from "../src/db/memory-source.js";
import type { Database } from "../src/db/mysql.js";
import { createHarnessServer, type HarnessServer } from "../src/server/app.js";
import { signHs256 } from "../src/business/jwt-hs256.js";
import { resolveSmartCsPaths, userJwtSecret, type SmartCsPaths } from "../src/config/env.js";
import { AuditDispatcher, AuditQueue } from "../src/tracing/audit-queue.js";
import {
  getFinishedSpans,
  resetTracingForTests,
  tracingMode,
} from "../src/tracing/provider.js";
import { ATTR } from "../src/tracing/spans.js";
import { formatSpanTree } from "../src/tracing/format.js";
import { makeTmpDir } from "./helpers/harness.js";
import { ticketCount } from "./helpers/f-matrix.js";
import { startToolProxy, type ToolProxy } from "./helpers/tool-proxy.js";
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
  process.env.INTERNAL_SERVICE_JWT_SECRET ?? "phase6-observability-secret-0123456789";

const PY_PORT = 8_910;
const PROXY_PORT = 8_920;
const SESSION_ID = "phase6-observability-session";
const ORDER_ID = "ORD-20260801-0002"; // belongs to user_002

let python: PythonService;
let db: Database;
let receipts: ReceiptStore;
let memorySource: MemorySourceStore;
let root: string;
let paths: SmartCsPaths;
let faux: FauxProviderRegistration;
let account: SeededAccount;
let orderDbPath: string;
let harnessPort = 8_911;
let harness: HarnessServer | undefined;
let activeProxy: ToolProxy | undefined;

function userToken(accountId: number, ttl = 1800): string {
  const now = Math.floor(Date.now() / 1000);
  return signHs256(
    { sub: String(accountId), iat: now, exp: now + ttl, iss: "smartcs", jti: `jti-${now}-${Math.random()}` },
    userJwtSecret(),
  );
}

async function buildHarness(
  baseUrl: string,
  options: { auditQueue?: AuditQueue; writeMode?: "off" | "live" } = {},
): Promise<HarnessServer> {
  const client = new PythonInternalClient({ baseUrl });
  const registry = new SessionRegistry(async (sessionId) => {
    const handle = await openOrCreatePiSession(paths, sessionId, {
      provider: "faux",
      faux,
      toolMode: "business",
      pythonClient: client,
      writeMode: options.writeMode ?? "live",
      receiptStore: receipts,
      ...(options.auditQueue ? { auditQueue: options.auditQueue } : {}),
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
    pythonClient: client,
    idleEvictionMs: 15 * 60_000,
  });
  harnessPort += 1;
  await server.listen(harnessPort);
  return server;
}

/** Replace the running harness; only one may hold a session file at a time. */
async function rebuildHarness(
  baseUrl: string,
  options: { auditQueue?: AuditQueue; writeMode?: "off" | "live" } = {},
): Promise<HarnessServer> {
  await harness?.close();
  harness = await buildHarness(baseUrl, options);
  return harness;
}

async function chat(
  clientRequestId: string,
  message: string,
  extraHeaders: Record<string, string> = {},
): Promise<{ status: number; body: Record<string, unknown> }> {
  const response = await fetch(`http://127.0.0.1:${harnessPort}/api/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${userToken(account.accountId)}`,
      ...extraHeaders,
    },
    body: JSON.stringify({ session_id: SESSION_ID, client_request_id: clientRequestId, message }),
  });
  return { status: response.status, body: (await response.json()) as Record<string, unknown> };
}

/** Python-side span records for one session (the in-process recorder's ring). */
async function pythonTraceRecords(sessionId: string, clientRequestId: string) {
  const token = mintServiceToken({
    accountId: account.accountId,
    businessUserId: account.businessUserId,
    sessionId,
    clientRequestId,
  });
  const response = await fetch(`${python.url}/internal/trace/records?limit=200`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(response.status).toBe(200);
  const body = (await response.json()) as { records: Array<Record<string, unknown>> };
  return body.records;
}

async function auditRowsForTrace(traceId: string): Promise<RowDataPacket[]> {
  return db.query<RowDataPacket>(
    "SELECT event_id, kind, tool_name, tool_call_id, operation_id, trace_id, session_id, client_request_id, payload FROM audit_event WHERE trace_id = ? ORDER BY id",
    [traceId],
  );
}

/**
 * Human-readable span tree, printed only when SMARTCS_DUMP_SPANS=1.
 *
 * It exists so the Phase 6 report can quote what the exporter actually held
 * rather than what the assertions were expected to find.
 */
function dumpSpanTree(traceId: string): void {
  if (process.env.SMARTCS_DUMP_SPANS !== "1") return;
  // Same renderer the offline diagnostic uses (scripts/phase6-span-tree.ts).
  console.log(
    `\n--- span tree for trace ${traceId} ---\n` +
      `${formatSpanTree(getFinishedSpans(), { traceId, skipAttributes: new Set([ATTR.traceId]) })}\n--- end ---`,
  );
}

function turnSpanFor(clientRequestId: string) {
  return getFinishedSpans().find(
    (span) =>
      span.name === "smartcs.agent.turn" &&
      span.attributes[ATTR.clientRequestId] === clientRequestId,
  );
}

beforeAll(async () => {
  // The default mode (no collector configured) is the in-process ring, which is
  // exactly what these assertions read. A developer's shell must not be able to
  // redirect them at a real collector.
  delete process.env.OTEL_EXPORTER_OTLP_ENDPOINT;
  delete process.env.OTEL_SDK_DISABLED;
  resetTracingForTests();

  await resetTestDatabase();
  db = testDatabase();
  receipts = new ReceiptStore(db);
  memorySource = new MemorySourceStore(db);
  account = await seedAccount(db, { username: "phase6-owner", businessUserId: "user_002" });
  await seedSession(db, { sessionId: SESSION_ID, accountId: account.accountId, harnessVersion: "pi" });

  root = makeTmpDir("phase6-obs-");
  orderDbPath = join(root, "orders.db");
  mkdirSync(root, { recursive: true });
  paths = resolveSmartCsPaths({
    runtimeCwd: join(root, "cwd"),
    sessionDir: join(root, "sessions"),
    agentDir: join(root, "agent"),
  });
  mkdirSync(paths.runtimeCwd, { recursive: true });
  mkdirSync(paths.sessionDir, { recursive: true });

  // Live write mode on the runtime side: P6-1 needs a real operation id.
  python = await startPythonService({
    port: PY_PORT,
    orderDbPath,
    writeMode: "live",
    traceRecords: true,
  });
  faux = createFauxProvider();
}, 120_000);

afterAll(async () => {
  await harness?.close();
  await activeProxy?.stop();
  faux?.unregister();
  await python?.stop();
  await db?.close();
  try {
    rmSync(root, { recursive: true, force: true });
  } catch {
    /* temp dirs are disposable */
  }
});

describe("Phase 6 observability", () => {
  it("P6-1: the TS turn span and the Python internal spans share one trace id", async () => {
    harness = await rebuildHarness(python.url);
    faux.setResponses([
      fauxAssistantMessage([fauxToolCall("order_query", { order_id: ORDER_ID } as never)], {
        stopReason: "toolUse",
      }),
      fauxAssistantMessage("订单已查询。"),
    ]);

    const result = await chat("req-p61-read", `帮我查一下订单 ${ORDER_ID}`);
    expect(result.status).toBe(200);

    // --- TS side: the turn span and its children -------------------------
    const turn = turnSpanFor("req-p61-read");
    expect(turn, "the memory exporter must hold the turn span").toBeDefined();
    const traceId = turn!.spanContext().traceId;
    expect(traceId).toMatch(/^[0-9a-f]{32}$/);
    expect(turn!.attributes[ATTR.sessionId]).toBe(SESSION_ID);
    expect(turn!.attributes[ATTR.agentRunId]).toBeDefined();
    expect(turn!.attributes[ATTR.intentLabel]).toBe("order");

    const spans = getFinishedSpans();
    const modelSpans = spans.filter((s) => s.spanContext().traceId === traceId && s.name === "smartcs.model.call");
    const toolSpans = spans.filter((s) => s.spanContext().traceId === traceId && s.name === "smartcs.tool.call");
    const complianceSpans = spans.filter(
      (s) => s.spanContext().traceId === traceId && s.name === "smartcs.compliance.review",
    );
    expect(modelSpans.length).toBeGreaterThan(0);
    expect(toolSpans.length).toBe(1);
    // The design's span tree has exactly these three branches, all under the
    // turn span.
    expect(complianceSpans.length).toBeGreaterThan(0);
    expect(complianceSpans[0]!.parentSpanContext?.spanId).toBe(turn!.spanContext().spanId);
    // Children point at the turn span, so the tree is one trace, not one trace
    // id with unrelated roots.
    expect(modelSpans[0]!.parentSpanContext?.spanId).toBe(turn!.spanContext().spanId);
    expect(toolSpans[0]!.parentSpanContext?.spanId).toBe(turn!.spanContext().spanId);
    expect(toolSpans[0]!.attributes[ATTR.tool]).toBe("order_query");
    const toolCallId = toolSpans[0]!.attributes[ATTR.toolCallId];
    expect(typeof toolCallId).toBe("string");

    // --- Python side: the same trace, the same parent ---------------------
    const records = await pythonTraceRecords(SESSION_ID, "req-p61-read");
    const forTrace = records.filter((record) => record.trace_id === traceId);
    expect(forTrace.length, "every internal hop of this turn must be in the trace").toBeGreaterThan(0);

    const paths_ = forTrace.map((record) => record.path);
    expect(paths_).toContain("/internal/tools/execute");
    expect(paths_).toContain("/internal/auth/verify");
    for (const record of forTrace) {
      expect(record.parent_span_id).toBe(turn!.spanContext().spanId);
      expect(record.session_id).toBe(SESSION_ID);
    }

    const toolRecord = forTrace.find((record) => record.path === "/internal/tools/execute")!;
    expect(toolRecord.tool_call_id).toBe(toolCallId);
    expect(toolRecord.agent_run_id).toBe(String(turn!.attributes[ATTR.agentRunId]));

    dumpSpanTree(traceId);

    // The receipt keeps the trace id, so a replay stays attributable.
    const receiptRows = await db.query<RowDataPacket>(
      "SELECT response FROM agent_run_receipt WHERE session_id = ? AND client_request_id = ?",
      [SESSION_ID, "req-p61-read"],
    );
    expect(JSON.stringify(receiptRows[0]!.response)).toContain(traceId);
  }, 60_000);

  it("P6-1: an inbound traceparent is honoured (the gateway owns the trace)", async () => {
    await rebuildHarness(python.url);
    faux.setResponses([fauxAssistantMessage("收到。")]);

    const upstream = "4bf92f3577b34da6a3ce929d0e0e4736";
    const result = await chat("req-p61-parent", "你好", {
      traceparent: `00-${upstream}-00f067aa0ba902b7-01`,
    });
    expect(result.status).toBe(200);

    const turn = turnSpanFor("req-p61-parent");
    expect(turn!.spanContext().traceId).toBe(upstream);
    const records = await pythonTraceRecords(SESSION_ID, "req-p61-parent");
    expect(records.some((record) => record.trace_id === upstream)).toBe(true);
  }, 60_000);

  it("P6-1: a live write completes the six-id family (operation_id included)", async () => {
    const queue = new AuditQueue(100);
    await rebuildHarness(python.url, { auditQueue: queue });
    // The runtime authorizes a ticket write only when the durable user message
    // itself asks for one; the model's tool call is a request, not authority.
    // Phase 10 §③: the model sends business parameters only — `user_id`,
    // `client_request_id` and `request_payload_hash` are supplied server-side
    // at the live-write boundary and are no longer expressible here.
    faux.setResponses([
      fauxAssistantMessage(
        [
          fauxToolCall("ticket_create", {
            title: "包裹未收到",
            description: "客户反馈包裹长时间未收到，请协助处理。",
          } as never),
        ],
        { stopReason: "toolUse" },
      ),
      fauxAssistantMessage("工单已提交。"),
    ]);

    const ticketsBefore = ticketCount(orderDbPath);
    const result = await chat("req-p61-write", "请帮我创建工单：包裹一直没收到，麻烦尽快处理。");
    expect(result.status).toBe(200);
    expect(ticketCount(orderDbPath)).toBe(ticketsBefore + 1);

    const turn = turnSpanFor("req-p61-write")!;
    const traceId = turn.spanContext().traceId;
    const toolSpan = getFinishedSpans().find(
      (span) => span.spanContext().traceId === traceId && span.name === "smartcs.tool.call",
    )!;
    const operationId = toolSpan.attributes[ATTR.operationId];
    expect(typeof operationId).toBe("string");

    // Deliver the queued audit records now instead of waiting on the timer.
    const dispatcher = new AuditDispatcher({
      client: new PythonInternalClient({ baseUrl: python.url }),
      queue,
    });
    const summary = await dispatcher.flushOnce();
    expect(summary.failed).toBe(0);
    expect(summary.delivered).toBeGreaterThanOrEqual(2);

    const rows = await auditRowsForTrace(traceId);
    const kinds = rows.map((row) => String(row.kind));
    expect(kinds).toContain("tool_call");
    expect(kinds).toContain("tool_result");
    const resultRow = rows.find((row) => String(row.kind) === "tool_result")!;
    expect(String(resultRow.operation_id)).toBe(operationId);
    expect(String(resultRow.tool_call_id)).toBe(toolSpan.attributes[ATTR.toolCallId]);
    expect(String(resultRow.session_id)).toBe(SESSION_ID);
    expect(String(resultRow.client_request_id)).toBe("req-p61-write");

    // The Python span for the write carries everything it can know at request
    // time; the operation id is minted server-side during the call, so it
    // appears on the TS tool span and the audit row instead.
    const records = await pythonTraceRecords(SESSION_ID, "req-p61-write");
    const writeRecord = records.find(
      (record) => record.trace_id === traceId && record.path === "/internal/tools/execute",
    )!;
    expect(writeRecord.tool_call_id).toBe(toolSpan.attributes[ATTR.toolCallId]);
    expect(writeRecord.agent_run_id).toBe(String(turn.attributes[ATTR.agentRunId]));
  }, 90_000);

  it("P6-3: a slow audit endpoint never delays the chat (fire-and-forget)", async () => {
    const queue = new AuditQueue(100);
    activeProxy = await startToolProxy({
      port: PROXY_PORT,
      target: python.url,
      rules: [
        {
          matches: (context) => context.url.includes("/internal/audit"),
          delayBeforeForwardMs: 2_000,
        },
      ],
    });
    await rebuildHarness(activeProxy.url, { auditQueue: queue });
    faux.setResponses([
      fauxAssistantMessage([fauxToolCall("order_query", { order_id: ORDER_ID } as never)], {
        stopReason: "toolUse",
      }),
      fauxAssistantMessage("查到了。"),
    ]);

    const first = await chat("req-p63-warm", `查一下 ${ORDER_ID}`);
    expect(first.status).toBe(200);
    expect(queue.size).toBeGreaterThan(0);

    // The audit delivery is in flight (2s) while the next chat runs.
    let flushSettled = false;
    const flush = new AuditDispatcher({
      client: new PythonInternalClient({ baseUrl: activeProxy.url }),
      queue,
    })
      .flushOnce()
      .then((summary) => {
        flushSettled = true;
        return summary;
      });

    faux.setResponses([
      fauxAssistantMessage([fauxToolCall("order_query", { order_id: ORDER_ID } as never)], {
        stopReason: "toolUse",
      }),
      fauxAssistantMessage("又查到了。"),
    ]);
    const started = Date.now();
    const second = await chat("req-p63-fast", `再查一下 ${ORDER_ID}`);
    const elapsed = Date.now() - started;
    expect(second.status).toBe(200);
    // The decisive evidence is not the number: it is that the audit POST had
    // not even returned while the chat finished.
    expect(flushSettled, "the chat must not wait for the audit flush").toBe(false);
    expect(elapsed, `chat latency includes no audit wait (took ${elapsed}ms)`).toBeLessThan(2_000);

    // ...and the audit does land, just later.
    const summary = await flush;
    expect(summary.failed).toBe(0);
    expect(summary.delivered).toBeGreaterThan(0);
    const delivered = await db.query<RowDataPacket>(
      "SELECT COUNT(*) AS n FROM audit_event WHERE client_request_id = ?",
      ["req-p63-warm"],
    );
    expect(Number(delivered[0]!.n)).toBeGreaterThan(0);
  }, 90_000);

  it("P6-4: queue overflow drops and counts, and the chat is unaffected", async () => {
    // Unit level: overflow discards the NEWEST event and counts it, so the
    // queue always holds a contiguous, chronological prefix of the burst (the
    // audit trail loses its tail, never its beginning). The drop count is the
    // signal — never back-pressure, never unbounded growth.
    const tiny = new AuditQueue(3);
    const route = {
      accountId: 1,
      businessUserId: "user_002",
      sessionId: SESSION_ID,
      clientRequestId: "req-overflow",
    };
    for (let index = 0; index < 10; index += 1) {
      tiny.push(
        {
          eventId: `evt-${index}`,
          kind: "tool_call",
          toolName: "order_query",
          occurredAt: new Date().toISOString(),
        },
        route,
      );
    }
    expect(tiny.size).toBe(3);
    expect(tiny.dropped).toBe(7);
    expect(tiny.enqueued).toBe(3);
    expect(tiny.drain(10).map((event) => event.eventId)).toEqual(["evt-0", "evt-1", "evt-2"]);
    // An event without a request identity cannot be delivered; it is counted,
    // not silently shipped with someone else's credentials.
    expect(tiny.push({ eventId: "evt-x", kind: "tool_call", toolName: "order_query", occurredAt: "" }, undefined)).toBe(
      false,
    );
    expect(tiny.dropped).toBe(8);

    // And through a real chat: the tool events overflow a capacity-1 queue and
    // the request still succeeds.
    const queue = new AuditQueue(1);
    await rebuildHarness(python.url, { auditQueue: queue });
    faux.setResponses([
      fauxAssistantMessage([fauxToolCall("order_query", { order_id: ORDER_ID } as never)], {
        stopReason: "toolUse",
      }),
      fauxAssistantMessage("查完了。"),
    ]);
    const result = await chat("req-p64-live", `查 ${ORDER_ID}`);
    expect(result.status).toBe(200);
    expect((result.body.message as { content: string }).content).toBe("查完了。");
    expect(queue.dropped).toBeGreaterThan(0);
  }, 60_000);

  it("P6-5: with tracing disabled the chat behaves exactly as before", async () => {
    resetTracingForTests();
    process.env.OTEL_SDK_DISABLED = "true";
    try {
      expect(tracingMode()).toBe("off");
      await rebuildHarness(python.url);
      faux.setResponses([fauxAssistantMessage("关闭观测后的答复。")]);

      const result = await chat("req-p65-off", "观测关闭态测试");
      expect(result.status).toBe(200);
      expect((result.body.message as { content: string }).content).toBe("关闭观测后的答复。");
      expect(getFinishedSpans()).toEqual([]);

      // The receipt still completes with its trace id, and the ledger path is
      // untouched: no span was ever recorded, yet the request is authoritative.
      const rows = await db.query<RowDataPacket>(
        "SELECT status FROM agent_run_receipt WHERE session_id = ? AND client_request_id = ?",
        [SESSION_ID, "req-p65-off"],
      );
      expect(String(rows[0]!.status)).toBe("completed");
    } finally {
      delete process.env.OTEL_SDK_DISABLED;
      resetTracingForTests();
    }
  }, 60_000);
});
