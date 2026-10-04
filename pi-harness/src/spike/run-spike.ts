/**
 * Phase 0 spike runner.
 *
 * Demonstrates the full acceptance path in one process:
 *   provider wiring → agent loop → tool call → compliance → JSON final
 *   → dispose → reopen by id → context restored
 *
 * Usage:
 *   npx tsx src/spike/run-spike.ts "帮我查一下订单 1001"
 *   SMARTCS_PHASE0_PROVIDER=faux npx tsx src/spike/run-spike.ts "你好"
 */

import { createSmartCsAgent, createFauxProvider } from "../agent/create-smartcs-agent.js";
import { ChatStream } from "../streaming/chat-stream.js";
import { loadLlmConfigFromPythonEnv, resolveProviderMode } from "../config/env.js";
import { SessionManager } from "@earendil-works/pi-coding-agent";
import { fauxAssistantMessage, fauxText, fauxToolCall } from "@earendil-works/pi-ai/providers/faux";

const prompt = process.argv.slice(2).join(" ") || "帮我查一下订单 1001 现在到哪了？";
const SESSION_ID = "smartcs-phase0-spike";

async function main(): Promise<void> {
  const mode = resolveProviderMode();
  const cfg = loadLlmConfigFromPythonEnv();

  const faux = mode === "faux" ? createFauxProvider() : undefined;
  if (faux) {
    faux.setResponses([
      fauxAssistantMessage(
        [fauxText("好的，我帮你查一下订单。"), fauxToolCall("order_query", { orderId: "1001" })],
        { stopReason: "toolUse" },
      ),
      fauxAssistantMessage("你的订单已发货，正在派送中。"),
    ]);
  }

  const agent = await createSmartCsAgent({
    provider: mode,
    faux,
    sessionId: SESSION_ID,
  });

  console.log(
    JSON.stringify(
      {
        event: "startup",
        providerMode: mode,
        model: agent.modelId,
        baseUrl: cfg ? cfg.baseUrl : "(faux, offline)",
        keySource: cfg ? cfg.sourceFile : "(none)",
        runtimeCwd: agent.paths.runtimeCwd,
        sessionDir: agent.paths.sessionDir,
        agentDir: agent.paths.agentDir,
        sessionId: agent.session.sessionId,
        sessionFile: agent.session.sessionFile,
        activeTools: agent.session.getActiveToolNames(),
      },
      null,
      2,
    ),
  );

  const stream = new ChatStream(agent.session);
  const before = agent.audit.records.length;
  const result = await stream.run(prompt);

  const finalPayload = {
    event: "final",
    sessionId: agent.session.sessionId,
    settledCount: result.settledCount,
    aborted: result.aborted,
    final: { role: "assistant", content: result.finalText },
    frames: result.frames.map((f) => ({ ...f, at: new Date(f.at).toISOString() })),
    audit: agent.audit.records.slice(before),
    sessionFile: agent.sessionManager.getSessionFile(),
    entries: agent.sessionManager.getEntries().length,
  };
  console.log(JSON.stringify(finalPayload, null, 2));

  // --- reopen by id in a fresh runtime ---
  const file = agent.sessionManager.getSessionFile()!;
  agent.dispose();
  faux?.unregister();

  const reopened = SessionManager.open(file);
  const context = reopened.buildSessionContext();
  console.log(
    JSON.stringify(
      {
        event: "reopen",
        sessionId: reopened.getSessionId(),
        leafId: reopened.getLeafId(),
        entryCount: reopened.getEntries().length,
        contextRoles: context.messages.map((m) => (m as { role?: string }).role),
        lastAssistant:
          [...context.messages].reverse().find((m) => (m as { role?: string }).role === "assistant") ?? null,
      },
      null,
      2,
    ),
  );
}

main().catch((error) => {
  console.error(JSON.stringify({ event: "error", error: String(error?.stack ?? error) }));
  process.exitCode = 1;
});
