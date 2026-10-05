/**
 * W3C trace context for one harness request (plan v2 §6.9).
 *
 * The harness owns trace-id generation at the HTTP edge: an inbound
 * `traceparent` is honoured (so an upstream gateway can start the trace);
 * otherwise a fresh W3C-shaped context is minted. Every internal HTTP call to
 * the Business Runtime carries the current context as a `traceparent` header,
 * which is exactly what makes the TS turn span and the Python server span
 * share one trace id (acceptance P6-1).
 */

import { randomBytes } from "node:crypto";

export interface TraceIds {
  /** 32 lowercase hex chars. */
  traceId: string;
  /** 16 lowercase hex chars — the span the downstream call is a child of. */
  spanId: string;
  sampled: boolean;
}

const TRACEPARENT_PATTERN = /^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$/;
const ZERO_TRACE_ID = "0".repeat(32);
const ZERO_SPAN_ID = "0".repeat(16);

export function randomTraceId(): string {
  return randomBytes(16).toString("hex");
}

export function randomSpanId(): string {
  return randomBytes(8).toString("hex");
}

export function isValidTraceId(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{32}$/.test(value) && value !== ZERO_TRACE_ID;
}

export function isValidSpanId(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{16}$/.test(value) && value !== ZERO_SPAN_ID;
}

/**
 * Parse a `traceparent` header value. Anything malformed — wrong arity, bad
 * hex, the reserved all-zero ids, the forbidden `ff` version — is rejected as
 * "no parent", never guessed at.
 */
export function parseTraceparent(value: unknown): TraceIds | undefined {
  if (typeof value !== "string") return undefined;
  const match = TRACEPARENT_PATTERN.exec(value.trim().toLowerCase());
  if (!match) return undefined;
  const [, version, traceId, spanId, flags] = match;
  if (version === "ff") return undefined;
  if (!isValidTraceId(traceId) || !isValidSpanId(spanId)) return undefined;
  return { traceId, spanId, sampled: (Number.parseInt(flags!, 16) & 0x01) === 0x01 };
}

export function formatTraceparent(ids: TraceIds): string {
  return `00-${ids.traceId}-${ids.spanId}-${ids.sampled ? "01" : "00"}`;
}

/**
 * The context for a new turn. A valid upstream `traceparent` contributes its
 * trace id (the trace continues across hops); the span id is always fresh
 * because this harness is starting a new span.
 */
export function newTraceIds(parentTraceparent?: unknown): TraceIds {
  const parent = parseTraceparent(parentTraceparent);
  return {
    traceId: parent?.traceId ?? randomTraceId(),
    spanId: randomSpanId(),
    sampled: parent ? parent.sampled : true,
  };
}
