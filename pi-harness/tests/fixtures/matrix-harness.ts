/**
 * A REAL harness process for the F1–F14 fault matrix.
 *
 * It is the production wiring (`createHarnessServer` over `openOrCreatePiSession`,
 * real MySQL receipt log, real Python runtime, real Pi session files) with
 * exactly one substitution: the model is the Faux provider driven by a scripted
 * file, because the matrix measures the HARNESS's behaviour under faults, not a
 * model's decision quality.
 *
 * It exists as a separate process because the crash points (`hitCrashPoint`)
 * abort the process they run in — arming one inside the vitest worker would
 * kill the test runner itself.
 *
 * Everything is configuration by environment so the test side can restart the
 * same durable state after a crash.
 */

import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import { fauxAssistantMessage, fauxToolCall, type FauxResponseStep } from "@earendil-works/pi-ai/providers/faux";
import { registerFauxProvider } from "@earendil-works/pi-ai/compat";
import { openOrCreatePiSession } from "../../src/session/pi-session.js";
import { SessionRegistry } from "../../src/session/registry.js";
import { PythonInternalClient } from "../../src/business/python-client.js";
import { createDatabase } from "../../src/db/mysql.js";
import { ReceiptStore } from "../../src/db/receipts.js";
import { MemorySourceStore } from "../../src/db/memory-source.js";
import { resolveSmartCsPaths } from "../../src/config/env.js";
import { resolveWriteMode } from "../../src/agent/write-mode.js";
import { createHarnessServer } from "../../src/server/app.js";

type ScriptStep =
  | { kind: "text"; text: string }
  | {
      kind: "tool";
      calls: Array<{ name: string; arguments: Record<string, unknown> }>;
      /** Closing text; `null` chains straight into the next step's tool call. */
      text?: string | null;
    }
  | {
      /**
       * Context-sensitive decision: call `refund_confirm` when the newest user
       * message carries the confirmation phrase, otherwise answer with text.
       *
       * F10 needs this because the runtime's compaction step asks the SAME
       * provider for a summary; a positional script would be consumed by the
       * summariser and the confirm turn would get the wrong response. The
       * decision is still scripted — it just no longer depends on call order.
       */
      kind: "confirm_router";
      pending_action_id: string;
      confirm_token: string;
      text: string;
      else_text: string;
      /** How many router responses to install (the queue is finite). */
      repeat: number;
    };

function textOfContent(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content.map((block) => String((block as { text?: string }).text ?? "")).join("");
}

/**
 * The newest REAL user message.
 *
 * The Phase 3 turn snapshot is injected as a custom message, which surfaces as
 * user-role content AFTER the actual prompt — the same detail `src/server/main.ts`
 * handles. Skipping it is what keeps the router reading the user's words rather
 * than the injected business context.
 */
function lastUserText(context: { messages: Array<{ role?: string; content?: unknown }> }): string {
  const lastUser = [...context.messages]
    .reverse()
    .find((m) => m.role === "user" && !textOfContent(m.content).startsWith("[SmartCS 业务上下文快照"));
  return textOfContent(lastUser?.content);
}

function toFauxSteps(script: ScriptStep[]): FauxResponseStep[] {
  const steps: FauxResponseStep[] = [];
  for (const step of script) {
    if (step.kind === "text") {
      steps.push(fauxAssistantMessage(step.text));
      continue;
    }
    if (step.kind === "confirm_router") {
      const route = (context: { messages: Array<{ role?: string; content?: unknown }> }) =>
        lastUserText(context).includes(step.confirm_token)
          ? fauxAssistantMessage(
              [fauxToolCall("refund_confirm", { pending_action_id: step.pending_action_id } as never)],
              { stopReason: "toolUse" },
            )
          : fauxAssistantMessage(step.else_text);
      steps.push(...Array.from({ length: step.repeat }, () => route));
      continue;
    }
    steps.push(
      fauxAssistantMessage(
        step.calls.map((call) => fauxToolCall(call.name, call.arguments as never)),
        { stopReason: "toolUse" },
      ),
    );
    // A tool-calling turn needs a closing text turn, or the runtime would keep
    // asking the model after the tools returned. `text: null` means "the next
    // scripted call comes right after this tool result" — which is how a
    // multi-step model turn (look, then act) is expressed.
    if (step.text !== null) steps.push(fauxAssistantMessage(step.text ?? "好的，已处理。"));
  }
  return steps;
}

async function main(): Promise<void> {
  const port = Number(process.env.MATRIX_HARNESS_PORT);
  const scriptFile = process.env.MATRIX_SCRIPT_FILE!;
  const script = JSON.parse(readFileSync(scriptFile, "utf-8")) as ScriptStep[];
  const repeat = Number(process.env.MATRIX_SCRIPT_REPEAT ?? "4");

  // A small context window is how F10 forces the runtime's real compaction
  // path; the default keeps every other case on the stock model definition.
  const contextWindow = process.env.MATRIX_FAUX_CONTEXT_WINDOW
    ? Number(process.env.MATRIX_FAUX_CONTEXT_WINDOW)
    : undefined;
  const faux = registerFauxProvider({
    provider: "faux",
    api: "faux",
    models: [{ id: "faux-1", name: "Faux 1", contextWindow }],
  });
  const steps = toFauxSteps(script);
  faux.setResponses(Array.from({ length: repeat }, () => steps).flat());

  const paths = resolveSmartCsPaths({
    runtimeCwd: process.env.MATRIX_RUNTIME_CWD,
    sessionDir: process.env.MATRIX_SESSION_DIR,
    agentDir: process.env.MATRIX_AGENT_DIR,
  });
  const db = createDatabase({
    host: process.env.MYSQL_HOST ?? "127.0.0.1",
    port: Number(process.env.MYSQL_PORT ?? "3307"),
    database: process.env.MYSQL_DATABASE!,
    user: process.env.MYSQL_USER!,
    password: process.env.MYSQL_PASSWORD!,
  });
  const receipts = new ReceiptStore(db);
  const client = new PythonInternalClient({ baseUrl: process.env.PYTHON_INTERNAL_BASE_URL });
  const writeMode = resolveWriteMode();

  // F10 needs compaction to be reachable without a 128k context: the profile is
  // configurable so the test can set a threshold the transcript will cross.
  const reserveTokens = process.env.MATRIX_COMPACTION_RESERVE
    ? Number(process.env.MATRIX_COMPACTION_RESERVE)
    : undefined;
  const keepRecentTokens = process.env.MATRIX_COMPACTION_KEEP
    ? Number(process.env.MATRIX_COMPACTION_KEEP)
    : undefined;

  const registry = new SessionRegistry(async (sessionId) => {
    const handle = await openOrCreatePiSession(paths, sessionId, {
      provider: "faux",
      faux,
      toolMode: "business",
      pythonClient: client,
      writeMode,
      receiptStore: receipts,
      ...(reserveTokens || keepRecentTokens
        ? { compaction: { enabled: true, reserveTokens, keepRecentTokens } }
        : {}),
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
    pythonClient: client,
    idleEvictionMs: 60 * 60_000,
  });

  const { port: bound } = await harness.listen(port, "127.0.0.1");

  // Test-only control surface (F10). It exists so the matrix can trigger the
  // runtime's REAL `AgentSession.compact()` on demand instead of trying to
  // squeeze a 128k context model into a threshold it would cross by accident.
  // It is not part of the harness's HTTP edge and nothing in src/ knows it.
  const controlPort = process.env.MATRIX_CONTROL_PORT ? Number(process.env.MATRIX_CONTROL_PORT) : undefined;
  if (controlPort) {
    createServer((req, res) => {
      void (async () => {
        if (req.method !== "POST" || !req.url?.startsWith("/compact")) {
          res.writeHead(404).end();
          return;
        }
        const chunks: Buffer[] = [];
        for await (const chunk of req) chunks.push(chunk as Buffer);
        const body = JSON.parse(Buffer.concat(chunks).toString("utf-8") || "{}") as { session_id?: string };
        const live = body.session_id ? registry.get(body.session_id) : undefined;
        if (!live) {
          res.writeHead(404, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ detail: "session not found" }));
          return;
        }
        try {
          const result = await live.session.compact();
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ ok: true, summaryLength: String(result?.summary ?? "").length }));
        } catch (error) {
          res.writeHead(500, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ detail: String(error) }));
        }
      })();
    }).listen(controlPort, "127.0.0.1");
  }

  console.log(JSON.stringify({ event: "listening", port: bound, writeMode, controlPort }));
}

main().catch((error) => {
  console.error(JSON.stringify({ event: "fatal", error: String(error?.stack ?? error) }));
  process.exit(1);
});
