/**
 * Phase 0 SSE status channel (minimal, spike-only).
 *
 * Plan v2 §6.5: the realtime channel carries deterministic status frames; the
 * final answer is buffered, reviewed and only sent after `agent_settled`.
 *
 *   GET  /health                 -> { ok: true }
 *   GET  /chat/stream?q=<text>   -> SSE: status* / final / done
 *   POST /abort                  -> aborts the in-flight run
 *
 * NOT production: no JWT, no receipt, no per-session mutex (Phase 1).
 *
 * Usage: npx tsx src/spike/run-sse-server.ts   (PORT env, default 8971)
 */

import { createServer, type ServerResponse } from "node:http";
import { createSmartCsAgent, createFauxProvider } from "../agent/create-smartcs-agent.js";
import { ChatStream } from "../streaming/chat-stream.js";
import { resolveProviderMode } from "../config/env.js";
import { formatSseFrame } from "../streaming/status.js";
import { fauxAssistantMessage, fauxText, fauxToolCall } from "@earendil-works/pi-ai/providers/faux";

const PORT = Number(process.env.PORT ?? 8971);
const SESSION_ID = process.env.SMARTCS_SPIKE_SESSION_ID ?? "smartcs-phase0-sse";

async function main(): Promise<void> {
  const mode = resolveProviderMode();
  const faux = mode === "faux" ? createFauxProvider() : undefined;
  if (faux) {
    faux.setResponses([
      fauxAssistantMessage(
        [fauxText("好的，我帮你查一下。"), fauxToolCall("order_query", { orderId: "1001" })],
        { stopReason: "toolUse" },
      ),
      fauxAssistantMessage("你的订单已发货。"),
    ]);
  }

  const agent = await createSmartCsAgent({ provider: mode, faux, sessionId: SESSION_ID });
  let inFlight: ChatStream | undefined;

  const sse = (res: ServerResponse) => {
    res.writeHead(200, {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
    });
  };

  const server = createServer((req, res) => {
    const url = new URL(req.url ?? "/", `http://127.0.0.1:${PORT}`);

    if (url.pathname === "/health") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ ok: true, providerMode: mode, sessionId: agent.session.sessionId }));
      return;
    }

    if (url.pathname === "/chat/stream" && req.method === "GET") {
      const text = url.searchParams.get("q") ?? "帮我查一下订单 1001";
      sse(res);
      const stream = new ChatStream(agent.session, {
        onFrame: (frame) => res.write(formatSseFrame(frame)),
      });
      inFlight = stream;
      stream
        .run(text)
        .catch((error) => {
          res.write(
            formatSseFrame({ type: "done", at: Date.now(), reason: "error" }),
          );
          res.write(`event: error\ndata: ${JSON.stringify({ message: String(error?.message ?? error) })}\n\n`);
        })
        .finally(() => {
          inFlight = undefined;
          res.end();
        });
      req.on("close", () => {
        // Plan v2 §6.1: a dropped SSE connection must unsubscribe and abort.
        void stream.abort();
      });
      return;
    }

    if (url.pathname === "/abort" && req.method === "POST") {
      void inFlight?.abort();
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ aborted: Boolean(inFlight), note: "abort != business failure" }));
      return;
    }

    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "not found" }));
  });

  server.listen(PORT, "127.0.0.1", () => {
    console.log(
      JSON.stringify({
        event: "listening",
        url: `http://127.0.0.1:${PORT}`,
        providerMode: mode,
        sessionId: agent.session.sessionId,
        sessionFile: agent.session.sessionFile,
      }),
    );
  });

  const shutdown = () => {
    server.close();
    agent.dispose();
    faux?.unregister();
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
}

main().catch((error) => {
  console.error(String(error?.stack ?? error));
  process.exitCode = 1;
});
