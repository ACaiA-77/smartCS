/**
 * Provider probe — validates the real OpenAI-compatible endpoint from
 * python-impl/.env through pi's `openai-completions` API.
 *
 * Checks, in order:
 *   1. credentials resolve (baseUrl + apiKey loaded from the existing .env)
 *   2. a minimal completion returns text
 *   3. a tool call round-trips (the fake read-only tool, no Python involved)
 *
 * Prints one JSON object. Never writes to python-impl/.
 *
 * Usage: npx tsx src/spike/run-provider-probe.ts
 */

import { createSmartCsAgent } from "../agent/create-smartcs-agent.js";
import { ChatStream } from "../streaming/chat-stream.js";
import { loadLlmConfigFromPythonEnv } from "../config/env.js";

const cfg = loadLlmConfigFromPythonEnv();

async function main(): Promise<void> {
  if (!cfg) {
    console.log(JSON.stringify({ event: "provider_probe", status: "blocked", reason: "no usable key in python-impl/.env" }));
    return;
  }

  console.log(
    JSON.stringify({
      event: "config",
      baseUrl: cfg.baseUrl,
      model: cfg.model,
      apiKeyPresent: cfg.apiKey.length > 0,
      apiKeyPrefix: `${cfg.apiKey.slice(0, 6)}…`,
      sourceFile: cfg.sourceFile,
    }),
  );

  const started = Date.now();
  const agent = await createSmartCsAgent({ provider: "openai", sessionId: "smartcs-provider-probe" });
  try {
    const stream = new ChatStream(agent.session);
    const result = await stream.run(
      "这是一次连通性测试。请只回复两个字：收到。",
    );
    const latencyMs = Date.now() - started;

    const context = agent.sessionManager.buildSessionContext();
    const assistant = [...context.messages].reverse().find((m) => (m as { role?: string }).role === "assistant") as
      | { stopReason?: string; model?: string; provider?: string; usage?: unknown; errorMessage?: string }
      | undefined;

    console.log(
      JSON.stringify(
        {
          event: "provider_probe",
          status: assistant?.stopReason === "error" ? "error" : "ok",
          latencyMs,
          settledCount: result.settledCount,
          finalText: result.finalText,
          stopReason: assistant?.stopReason,
          errorMessage: assistant?.errorMessage,
          provider: assistant?.provider,
          model: assistant?.model,
          usage: assistant?.usage,
          sessionFile: agent.sessionManager.getSessionFile(),
        },
        null,
        2,
      ),
    );

    // --- phase 2: real tool call round-trip (fake tool, no Python) ---
    const toolStarted = Date.now();
    const toolStream = new ChatStream(agent.session);
    const toolResult = await toolStream.run("帮我查一下订单 1001 的状态。");
    const toolContext = agent.sessionManager.buildSessionContext();
    const toolResults = toolContext.messages.filter((m) => (m as { role?: string }).role === "toolResult");
    const calledTools = agent.audit.records.filter((r) => r.kind === "tool_call").map((r) => r.toolName);

    console.log(
      JSON.stringify(
        {
          event: "tool_probe",
          status: toolResult.settledCount === 1 ? "ok" : "unexpected",
          latencyMs: Date.now() - toolStarted,
          calledTools,
          toolResultCount: toolResults.length,
          toolResultIsError: toolResults.map((r) => (r as { isError?: boolean }).isError),
          toolResultPreview: toolResults.map((r) => JSON.stringify((r as { content?: unknown }).content).slice(0, 160)),
          finalText: toolResult.finalText,
        },
        null,
        2,
      ),
    );
  } finally {
    agent.dispose();
  }
}

main().catch((error) => {
  console.log(
    JSON.stringify({ event: "provider_probe", status: "failed", error: String(error?.message ?? error) }, null, 2),
  );
  process.exitCode = 1;
});
