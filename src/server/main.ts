/**
 * pi-harness entrypoint.
 *
 * Wires production configuration from the environment (python-impl/.env is the
 * shared source for secrets) and starts the HTTP edge.
 *
 * Usage: npm run dev        (PORT env, default 8971)
 */

import { SessionRegistry } from "../session/registry.js";
import { openOrCreatePiSession } from "../session/pi-session.js";
import { PythonInternalClient } from "../business/python-client.js";
import { createDatabase } from "../db/mysql.js";
import { ReceiptStore } from "../db/receipts.js";
import { MemorySourceStore } from "../db/memory-source.js";
import { resolveSmartCsPaths, resolveProviderMode } from "../config/env.js";
import { createHarnessServer } from "./app.js";
import { createFauxProvider } from "../agent/create-smartcs-agent.js";
import { fauxAssistantMessage } from "@earendil-works/pi-ai/providers/faux";
import { AuditDispatcher, AuditQueue } from "../tracing/audit-queue.js";
import { initTracing } from "../tracing/provider.js";

const PORT = Number(process.env.PORT ?? 8971);
const HOST = process.env.HOST ?? "127.0.0.1";
const IDLE_EVICTION_MS = Number(process.env.SMARTCS_IDLE_EVICTION_MS ?? 15 * 60_000);
const AUDIT_QUEUE_CAPACITY = Number(process.env.SMARTCS_AUDIT_QUEUE_CAPACITY ?? 1_000);
const AUDIT_BATCH_SIZE = Number(process.env.SMARTCS_AUDIT_BATCH_SIZE ?? 50);
const AUDIT_INTERVAL_MS = Number(process.env.SMARTCS_AUDIT_INTERVAL_MS ?? 1_000);

async function main(): Promise<void> {
  const paths = resolveSmartCsPaths();
  const db = createDatabase();
  const providerMode = resolveProviderMode();

  // Offline mode exists so a real OS process can be started, killed and
  // restarted in tests (F12) without reaching a live model. The queue is
  // filled with a factory so repeated prompts never exhaust it.
  const faux = providerMode === "faux" ? createFauxProvider() : undefined;
  if (faux) {
    const capacity = Number(process.env.SMARTCS_FAUX_CAPACITY ?? 200);
    const reply = (context: { messages: Array<{ role?: string; content?: unknown }> }) => {
      // Skip the Phase 3 turn snapshot: it is injected as a custom message,
      // which surfaces as user-role content in the model context.
      const textOf = (m: { content?: unknown }): string => {
        if (typeof m.content === "string") return m.content;
        if (!Array.isArray(m.content)) return "";
        return m.content
          .map((b) => (b as { type?: string; text?: string }).text ?? "")
          .join("");
      };
      const lastUser = [...context.messages]
        .reverse()
        .find((m) => m.role === "user" && !textOf(m).startsWith("[SmartCS 业务上下文快照"));
      const text =
        typeof lastUser?.content === "string"
          ? lastUser.content
          : Array.isArray(lastUser?.content)
            ? (lastUser.content as Array<{ type?: string; text?: string }>)
                .filter((b) => b?.type === "text")
                .map((b) => b.text ?? "")
                .join("")
            : "";
      return fauxAssistantMessage(`[faux] 收到：${text}`);
    };
    faux.setResponses(Array.from({ length: capacity }, () => reply));
  }

  const pythonClient = new PythonInternalClient();
  const receipts = new ReceiptStore(db);

  // Phase 6: spans are created synchronously on the request path; export is
  // owned by the SDK (batch to OTLP, or the in-process ring by default).
  const tracingMode = initTracing();
  // Phase 6 audit: the hook pushes, this loop delivers. Bounded on purpose —
  // overflow is dropped and counted, never back-pressured onto the request.
  const auditQueue = new AuditQueue(AUDIT_QUEUE_CAPACITY);
  const auditDispatcher = new AuditDispatcher({
    client: pythonClient,
    queue: auditQueue,
    batchSize: AUDIT_BATCH_SIZE,
    intervalMs: AUDIT_INTERVAL_MS,
    onError: () => undefined, // best-effort by contract; the counter is the signal
  });
  auditDispatcher.start();

  const registry = new SessionRegistry(async (sessionId) => {
    const handle = await openOrCreatePiSession(paths, sessionId, {
      provider: providerMode,
      faux,
      toolMode: "business",
      pythonClient,
      // Live writes need the durable operation log to reconcile a dropped
      // response; createSmartCsAgent refuses to start without it.
      receiptStore: receipts,
      auditQueue,
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
    memorySource: new MemorySourceStore(db),
    pythonClient,
    idleEvictionMs: IDLE_EVICTION_MS,
  });

  const { port } = await harness.listen(PORT, HOST);
  console.log(
    JSON.stringify({
      event: "listening",
      url: `http://${HOST}:${port}`,
      sessionDir: paths.sessionDir,
      runtimeCwd: paths.runtimeCwd,
      agentDir: paths.agentDir,
      providerMode,
      idleEvictionMs: IDLE_EVICTION_MS,
      tracingMode,
      auditQueue: { capacity: AUDIT_QUEUE_CAPACITY, dropped: auditQueue.dropped, enqueued: auditQueue.enqueued },
    }),
  );

  const shutdown = async () => {
    // Best-effort: a final drain is attempted, but a failure here must not
    // delay or block the shutdown itself.
    auditDispatcher.stop();
    void auditDispatcher.flushOnce().catch(() => undefined);
    await harness.close();
    await db.close();
    faux?.unregister();
    process.exit(0);
  };
  process.on("SIGINT", () => void shutdown());
  process.on("SIGTERM", () => void shutdown());
}

main().catch((error) => {
  console.error(JSON.stringify({ event: "fatal", error: String(error?.stack ?? error) }));
  process.exit(1);
});
