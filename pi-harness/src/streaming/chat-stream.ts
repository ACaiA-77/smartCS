/**
 * Chat streaming lifecycle (plan v2 §6.1/§6.5).
 *
 * Wires one prompt into a frame sequence:
 *   status (deterministic, from tool execution)
 *   → final (latest eligible, compliance-approved assistant message)
 *   → done (on agent_settled)
 *
 * Deliberate choices, both load-bearing in plan v2:
 *  - `agent_settled` is the completion signal, not `agent_end` (§6.1).
 *  - model narration never becomes a status frame; status text is a pure
 *    function of the tool name (§6.5).
 */

import type { AgentSession } from "@earendil-works/pi-coding-agent";
import {
  formatSseFrame,
  RunOutputBuffer,
  SAFE_FALLBACK_FINAL,
  statusForTool,
  type StreamFrame,
} from "./status.js";
import { ATTR, type SpanHandle, type TurnSpan } from "../tracing/spans.js";

export interface ChatStreamOptions {
  onFrame?: (frame: StreamFrame) => void;
  /**
   * Phase 6: the turn's span. Model and tool spans are its children. The
   * subscription path is where these events are visible, and nothing here
   * awaits an exporter — spans are created and ended synchronously.
   */
  spans?: TurnSpan;
}

export interface ChatStreamResult {
  frames: StreamFrame[];
  finalText: string;
  settledCount: number;
  aborted: boolean;
  /** Compaction events observed during the run (observability only). */
  compactionEvents: string[];
}

export class ChatStream {
  private readonly frames: StreamFrame[] = [];
  private readonly buffer = new RunOutputBuffer();
  private unsubscribe: (() => void) | undefined;
  private settled = 0;
  private aborted = false;
  private compactionEvents: string[] = [];
  private settledResolve: (() => void) | undefined;
  private settledPromise: Promise<void> | undefined;
  // Phase 6 span bookkeeping (empty when no turn span was supplied).
  private modelSpans = new Map<number, SpanHandle>();
  private toolSpans = new Map<string, SpanHandle>();
  private modelCallIndex = 0;

  constructor(
    private readonly session: AgentSession,
    private readonly options: ChatStreamOptions = {},
  ) {}

  private push(frame: StreamFrame): void {
    this.frames.push(frame);
    this.options.onFrame?.(frame);
  }

  /** Close any span a crashed/short-circuited run left open. */
  private endOpenSpans(): void {
    for (const span of this.modelSpans.values()) span.end();
    this.modelSpans.clear();
    for (const span of this.toolSpans.values()) span.end();
    this.toolSpans.clear();
  }

  /** Subscribe for one run. Returns the unsubscribe function. */
  start(): () => void {
    this.unsubscribe?.();
    this.unsubscribe = this.session.subscribe((event) => {
      // The public listener runs AFTER the extension layer, so the message seen
      // here is already the compliance-approved one (A2/A3).
      if (event.type === "message_start") {
        const message = (event as { message?: { role?: string } }).message;
        if (message?.role === "assistant") {
          this.modelCallIndex += 1;
          const span = this.options.spans?.child("smartcs.model.call", {
            [ATTR.modelCallIndex]: this.modelCallIndex,
          });
          if (span) this.modelSpans.set(this.modelCallIndex, span);
        }
        return;
      }
      if (event.type === "message_end") {
        const message = (event as { message?: { role?: string; usage?: { input?: number; output?: number } } })
          .message;
        this.buffer.record((event as { message: unknown }).message);
        if (message?.role === "assistant") {
          const span = this.modelSpans.get(this.modelCallIndex);
          this.modelSpans.delete(this.modelCallIndex);
          if (span) {
            if (typeof message.usage?.input === "number") span.setAttribute(ATTR.inputTokens, message.usage.input);
            if (typeof message.usage?.output === "number") span.setAttribute(ATTR.outputTokens, message.usage.output);
            span.end();
          }
        }
        return;
      }
      if (event.type === "tool_execution_start") {
        const toolName = String((event as { toolName?: string }).toolName ?? "");
        const toolCallId = String((event as { toolCallId?: string }).toolCallId ?? "");
        const span = this.options.spans?.child("smartcs.tool.call", {
          [ATTR.tool]: toolName,
          ...(toolCallId ? { [ATTR.toolCallId]: toolCallId } : {}),
        });
        if (span) this.toolSpans.set(toolCallId || `${toolName}:${this.toolSpans.size}`, span);
        this.push({ type: "status", text: statusForTool(toolName), at: Date.now() });
        return;
      }
      if (event.type === "tool_execution_end") {
        const toolCallId = String((event as { toolCallId?: string }).toolCallId ?? "");
        const span = this.toolSpans.get(toolCallId);
        if (span) {
          this.toolSpans.delete(toolCallId);
          span.setAttribute(ATTR.toolIsError, (event as { isError?: boolean }).isError === true);
          const details = (event as { result?: { details?: unknown } }).result?.details;
          const operationId =
            details && typeof details === "object"
              ? (details as { operationId?: unknown }).operationId
              : undefined;
          if (typeof operationId === "string" && operationId) {
            span.setAttribute(ATTR.operationId, operationId);
          }
          span.end();
        }
        return;
      }
      if (event.type === "agent_settled") {
        this.settled += 1;
        this.settledResolve?.();
        return;
      }
      // `compaction_*` is subscribe-only in 1.0.x, so this listener is the
      // right place to observe it (Phase 0 §sim. events doc).
      if (event.type.startsWith("compaction")) {
        this.compactionEvents.push(event.type);
      }
    });
    return () => this.unsubscribe?.();
  }

  /**
   * Emit the buffered final and done. Called on settle; also called from a
   * `finally` so a missing settle can never hang the connection.
   */
  finish(): ChatStreamResult {
    this.endOpenSpans();
    const finalText = this.buffer.take() ?? SAFE_FALLBACK_FINAL;
    this.push({ type: "final", text: finalText, at: Date.now() });
    this.push({ type: "done", at: Date.now(), reason: this.aborted ? "aborted" : "settled" });
    return this.result();
  }

  result(): ChatStreamResult {
    return {
      frames: [...this.frames],
      finalText: this.frames.filter((f) => f.type === "final").at(-1)?.text ?? "",
      settledCount: this.settled,
      aborted: this.aborted,
      compactionEvents: [...this.compactionEvents],
    };
  }

  /** Run one prompt to completion; always emits done exactly once. */
  async run(text: string): Promise<ChatStreamResult> {
    this.buffer.reset();
    this.settled = 0;
    this.aborted = false;
    this.compactionEvents = [];
    this.settledPromise = new Promise<void>((resolve) => {
      this.settledResolve = resolve;
    });
    const unsubscribe = this.start();
    try {
      await this.session.prompt(text);
    } finally {
      // If the SDK never settled (crash, unexpected throw), do not hang.
      unsubscribe();
      return this.finish();
    }
  }

  /** Abort the in-flight run. abort() != business failure (plan v2 §6.3). */
  async abort(): Promise<void> {
    this.aborted = true;
    await this.session.abort();
  }

  toSse(): string {
    return this.frames.map(formatSseFrame).join("");
  }
}
