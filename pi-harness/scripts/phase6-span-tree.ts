/**
 * Phase 6 diagnostic: print the span tree a chat turn produces.
 *
 * This drives the REAL production objects — `startTurnSpan`, `ChatStream` and
 * its `session.subscribe` wiring, the in-memory exporter — with a scripted
 * stand-in for the pi runtime's event stream, so it needs no MySQL, no Python
 * and no model. What it proves is the span SHAPE and the attribute set that the
 * end-to-end acceptance case (P6-1) asserts on a real pipeline; use that case
 * for the full chain (it also checks the Python side shares the trace id).
 *
 * Usage: npx tsx scripts/phase6-span-tree.ts
 */

import type { AgentSession } from "@earendil-works/pi-coding-agent";
import { ChatStream } from "../src/streaming/chat-stream.js";
import { getFinishedSpans, initTracing } from "../src/tracing/provider.js";
import { ATTR, startTurnSpan } from "../src/tracing/spans.js";
import { formatSpanTree } from "../src/tracing/format.js";

/** `smartcs.trace_id` duplicates the span context; it is noise in a dump. */
const SKIP_ATTRIBUTES = new Set([ATTR.traceId]);

type Listener = (event: unknown) => void;

/** The smallest thing ChatStream needs: `subscribe` and `prompt`. */
class ScriptedSession {
  private listeners: Listener[] = [];

  subscribe(listener: Listener): () => void {
    this.listeners.push(listener);
    return () => {
      this.listeners = this.listeners.filter((item) => item !== listener);
    };
  }

  private emit(event: unknown): void {
    for (const listener of [...this.listeners]) listener(event);
  }

  async prompt(): Promise<void> {
    // One assistant message that calls a read tool, then a closing answer —
    // the same shape the acceptance case drives through the real runtime.
    this.emit({ type: "message_start", message: { role: "assistant" } });
    this.emit({
      type: "message_end",
      message: { role: "assistant", content: [], usage: { input: 812, output: 36 } },
    });
    this.emit({
      type: "tool_execution_start",
      toolCallId: "call_01_phase6_demo",
      toolName: "order_query",
      args: { order_id: "ORD-20260801-0002" },
    });
    this.emit({
      type: "tool_execution_end",
      toolCallId: "call_01_phase6_demo",
      toolName: "order_query",
      result: { details: { found: true } },
      isError: false,
    });
    this.emit({ type: "message_start", message: { role: "assistant" } });
    this.emit({
      type: "message_end",
      message: { role: "assistant", content: [{ type: "text", text: "订单 ORD-20260801-0002 已发货。" }], usage: { input: 903, output: 52 } },
    });
    this.emit({ type: "agent_settled" });
  }

  async abort(): Promise<void> {
    /* nothing to abort in the scripted session */
  }
}

async function main(): Promise<void> {
  const mode = initTracing();
  if (mode !== "memory") {
    throw new Error(`this diagnostic reads the in-memory exporter; mode is "${mode}"`);
  }

  const turn = startTurnSpan({
    sessionId: "phase6-demo-session",
    clientRequestId: "phase6-demo-request",
  });
  turn.setAgentRunId("4242");
  turn.setAttribute(ATTR.intentLabel, "order");

  const stream = new ChatStream(new ScriptedSession() as unknown as AgentSession, { spans: turn });
  const result = await stream.run("我的订单到哪了？");

  // The compliance review runs inside a pi hook and hangs its span off the
  // propagated context instead of a span object (see create-smartcs-agent.ts).
  const compliance = turn.child("smartcs.compliance.review", {
    [ATTR.sessionId]: "phase6-demo-session",
    [ATTR.clientRequestId]: "phase6-demo-request",
    [ATTR.agentRunId]: "4242",
  });
  compliance.setAttribute("smartcs.compliance.verdict", "pass");
  compliance.end();

  turn.setAttribute(ATTR.replayed, false);
  turn.end();

  console.log(`traceparent sent downstream: ${turn.traceparent}`);
  console.log(`frames emitted: ${result.frames.length} (final=${result.finalText.length} chars)`);
  console.log("--- span tree ---");
  console.log(formatSpanTree(getFinishedSpans(), { traceId: turn.traceId, skipAttributes: SKIP_ATTRIBUTES }));
}

main().catch((error) => {
  console.error(String(error?.stack ?? error));
  process.exit(1);
});
