/**
 * Span construction for the harness (phase6-design.md §2).
 *
 *   smartcs.agent.turn            session_id · client_request_id · agent_run_id · intent_label
 *     ├ smartcs.model.call        one per assistant message
 *     ├ smartcs.tool.call         tool · tool_call_id · operation_id · is_error
 *     └ smartcs.compliance.review one per reviewed final answer
 *
 * Every span carries the ids that were actually known at the time it was
 * started — the six-id family is not decorative: session_id/client_request_id
 * come from the turn, agent_run_id from the receipt row, tool_call_id from Pi,
 * and operation_id from the Business Runtime's write ledger.
 */

import {
  ROOT_CONTEXT,
  TraceFlags,
  context,
  trace,
  type Span,
  type SpanContext,
} from "@opentelemetry/api";
import { getTracer } from "./provider.js";
import {
  formatTraceparent,
  isValidSpanId,
  isValidTraceId,
  newTraceIds,
  parseTraceparent,
} from "./trace-context.js";

export type AttributeValue = string | number | boolean;

/** Attribute keys. Prefixed so they cannot collide with OTel conventions. */
export const ATTR = {
  traceId: "smartcs.trace_id",
  sessionId: "smartcs.session_id",
  clientRequestId: "smartcs.client_request_id",
  agentRunId: "smartcs.agent_run_id",
  toolCallId: "smartcs.tool_call_id",
  operationId: "smartcs.operation_id",
  tool: "smartcs.tool",
  toolIsError: "smartcs.tool.is_error",
  modelCallIndex: "smartcs.model.call_index",
  inputTokens: "smartcs.model.input_tokens",
  outputTokens: "smartcs.model.output_tokens",
  stopReason: "smartcs.model.stop_reason",
  intentLabel: "smartcs.intent_label",
  replayed: "smartcs.replayed",
  recoveredFrom: "smartcs.recovered_from",
  outcome: "smartcs.outcome",
} as const;

export interface SpanHandle {
  readonly recording: boolean;
  setAttribute(key: string, value: AttributeValue): void;
  setAttributes(values: Record<string, AttributeValue>): void;
  end(): void;
}

export interface TurnSpan extends SpanHandle {
  readonly traceId: string;
  /** What every internal HTTP call of this turn sends downstream. */
  readonly traceparent: string;
  /** Recorded once the receipt row exists (that row id IS the agent_run_id). */
  setAgentRunId(agentRunId: string): void;
  /** A child of the turn span; a no-op handle when tracing is disabled. */
  child(name: string, attributes?: Record<string, AttributeValue>): SpanHandle;
}

const NOOP_HANDLE: SpanHandle = {
  recording: false,
  setAttribute: () => undefined,
  setAttributes: () => undefined,
  end: () => undefined,
};

function isRecordingSpan(span: Span): boolean {
  return span.isRecording();
}

function handleFor(span: Span): SpanHandle {
  if (!isRecordingSpan(span)) return NOOP_HANDLE;
  return {
    recording: true,
    setAttribute: (key, value) => span.setAttribute(key, value),
    setAttributes: (values) => span.setAttributes(values),
    end: () => span.end(),
  };
}

function parentContextFromTraceparent(traceparent: unknown) {
  const parsed = parseTraceparent(traceparent);
  if (!parsed) return undefined;
  const spanContext: SpanContext = {
    traceId: parsed.traceId,
    spanId: parsed.spanId,
    traceFlags: parsed.sampled ? TraceFlags.SAMPLED : TraceFlags.NONE,
    isRemote: true,
  };
  return trace.setSpanContext(ROOT_CONTEXT, spanContext);
}

/**
 * Start the turn span. The returned `traceparent` is what goes on the wire to
 * Python, so with tracing enabled it names THIS span — the Python server span
 * then becomes its child and the two share a trace id.
 */
export function startTurnSpan(input: {
  traceparent?: string;
  sessionId: string;
  clientRequestId: string;
}): TurnSpan {
  const tracer = getTracer();
  const parent = parentContextFromTraceparent(input.traceparent);
  const span = tracer.startSpan(
    "smartcs.agent.turn",
    {
      attributes: {
        [ATTR.sessionId]: input.sessionId,
        [ATTR.clientRequestId]: input.clientRequestId,
      },
    },
    parent,
  );

  const spanContext = span.spanContext();
  // With a recording span the trace ids are the SDK's; otherwise (SDK disabled)
  // mint a W3C-shaped context so correlation still works end to end.
  const ids = isValidTraceId(spanContext.traceId) && isValidSpanId(spanContext.spanId)
    ? {
        traceId: spanContext.traceId,
        spanId: spanContext.spanId,
        sampled: (spanContext.traceFlags & TraceFlags.SAMPLED) === TraceFlags.SAMPLED,
      }
    : newTraceIds(input.traceparent);

  span.setAttribute(ATTR.traceId, ids.traceId);

  return {
    traceId: ids.traceId,
    traceparent: formatTraceparent(ids),
    recording: isRecordingSpan(span),
    setAttribute: (key, value) => span.setAttribute(key, value),
    setAttributes: (values) => span.setAttributes(values),
    setAgentRunId: (agentRunId) => span.setAttribute(ATTR.agentRunId, agentRunId),
    child(name, attributes) {
      if (!isRecordingSpan(span)) return NOOP_HANDLE;
      const child = tracer.startSpan(name, { attributes }, trace.setSpan(context.active(), span));
      return handleFor(child);
    },
    end: () => span.end(),
  };
}

/**
 * Start a span whose parent is given by a `traceparent` string rather than by
 * an in-memory span object. Used where the caller only has the propagated
 * context (the compliance reviewer runs inside a pi hook, not next to the
 * pipeline's span handle).
 */
export function startSpanFromTraceparent(
  name: string,
  traceparent: string | undefined,
  attributes: Record<string, AttributeValue> = {},
): SpanHandle {
  const parent = parentContextFromTraceparent(traceparent);
  if (!parent) return NOOP_HANDLE;
  const span = getTracer().startSpan(name, { attributes }, parent);
  return handleFor(span);
}
