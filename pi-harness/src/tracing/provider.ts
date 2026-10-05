/**
 * OpenTelemetry wiring for the harness (plan v2 §6.9, phase6-design.md §2).
 *
 * Three modes, chosen from the standard OTEL_* environment variables:
 *
 *   OTEL_SDK_DISABLED=1            → "off"    no provider at all; every span is
 *                                              a no-op. Behaviour is bit-for-bit
 *                                              Phase 5 (P6-5).
 *   OTEL_EXPORTER_OTLP_ENDPOINT=…  → "otlp"   BatchSpanProcessor + OTLP/HTTP.
 *                                              Export happens on the SDK's own
 *                                              timer thread, never in the hook.
 *   (neither)                      → "memory" a bounded in-process ring. This is
 *                                              the default and the mode the
 *                                              acceptance tests assert on.
 *
 * Red line (design §2): nothing here may block a pi hook. `startSpan`/`end` are
 * synchronous; the only exporter that touches the network does so from the
 * batch processor, and the memory exporter is an O(1) array push.
 */

import { trace, type Tracer } from "@opentelemetry/api";
import { ExportResultCode, type ExportResult } from "@opentelemetry/core";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { resourceFromAttributes } from "@opentelemetry/resources";
import {
  BasicTracerProvider,
  BatchSpanProcessor,
  SimpleSpanProcessor,
  type ReadableSpan,
  type SpanExporter,
} from "@opentelemetry/sdk-trace-base";

export const TRACER_NAME = "smartcs-pi-harness";

/** Bounded so a long-running process without an exporter cannot grow forever. */
export const DEFAULT_MEMORY_SPAN_CAPACITY = 1_000;

export type TracingMode = "off" | "otlp" | "memory";

/** In-process span sink for the default (no-collector) mode and for tests. */
export class BoundedMemoryExporter implements SpanExporter {
  private spans: ReadableSpan[] = [];
  private dropped = 0;

  constructor(private readonly capacity = DEFAULT_MEMORY_SPAN_CAPACITY) {}

  export(batch: ReadableSpan[], resultCallback: (result: ExportResult) => void): void {
    for (const span of batch) {
      this.spans.push(span);
      // Oldest-first eviction: the ring keeps the most recent trace, which is
      // what a "what just happened" diagnostic needs.
      while (this.spans.length > this.capacity) {
        this.spans.shift();
        this.dropped += 1;
      }
    }
    resultCallback({ code: ExportResultCode.SUCCESS });
  }

  shutdown(): Promise<void> {
    return Promise.resolve();
  }

  getFinishedSpans(): ReadableSpan[] {
    return [...this.spans];
  }

  getDropped(): number {
    return this.dropped;
  }

  reset(): void {
    this.spans = [];
    this.dropped = 0;
  }
}

interface TracingState {
  mode: TracingMode;
  tracer: Tracer;
  provider?: BasicTracerProvider;
  memory?: BoundedMemoryExporter;
}

let state: TracingState | undefined;

function truthy(value: string | undefined): boolean {
  return ["1", "true", "yes", "on"].includes((value ?? "").trim().toLowerCase());
}

function otlpTraceUrl(endpoint: string): string {
  const trimmed = endpoint.replace(/\/+$/, "");
  return /\/v1\/traces$/.test(trimmed) ? trimmed : `${trimmed}/v1/traces`;
}

function build(options: { serviceName?: string; endpoint?: string; disabled?: boolean }): TracingState {
  const serviceName = options.serviceName ?? process.env.OTEL_SERVICE_NAME ?? "smartcs-pi-harness";
  const disabled = options.disabled ?? truthy(process.env.OTEL_SDK_DISABLED);
  const endpoint = options.endpoint ?? process.env.OTEL_EXPORTER_OTLP_ENDPOINT;

  if (disabled) {
    // No provider is registered: the API hands out no-op spans.
    return { mode: "off", tracer: trace.getTracer(TRACER_NAME) };
  }

  const memory = endpoint ? undefined : new BoundedMemoryExporter();
  const provider = new BasicTracerProvider({
    resource: resourceFromAttributes({ "service.name": serviceName }),
    spanProcessors: [
      endpoint
        ? new BatchSpanProcessor(new OTLPTraceExporter({ url: otlpTraceUrl(endpoint) }))
        : new SimpleSpanProcessor(memory!),
    ],
  });
  trace.setGlobalTracerProvider(provider);
  return { mode: endpoint ? "otlp" : "memory", tracer: provider.getTracer(TRACER_NAME), provider, memory };
}

/**
 * Idempotent. Callers never have to: `getTracer()` initialises on first use, so
 * a test or a server that forgets to call this still gets the documented
 * default (memory).
 */
export function initTracing(options: { serviceName?: string; endpoint?: string; disabled?: boolean } = {}): TracingMode {
  state = state ?? build(options);
  return state.mode;
}

export function getTracer(): Tracer {
  initTracing();
  return state!.tracer;
}

export function tracingMode(): TracingMode {
  return initTracing();
}

/** Spans the in-memory exporter holds, oldest first (tests assert on these). */
export function getFinishedSpans(): ReadableSpan[] {
  initTracing();
  return state!.memory?.getFinishedSpans() ?? [];
}

export function getDroppedSpanCount(): number {
  initTracing();
  return state!.memory?.getDropped() ?? 0;
}

/** Spans of one trace id, in export order. */
export function getSpansForTrace(traceId: string): ReadableSpan[] {
  return getFinishedSpans().filter((span) => span.spanContext().traceId === traceId);
}

/** Best-effort flush; only meaningful for the batching (OTLP) exporter. */
export async function forceFlushTracing(): Promise<void> {
  try {
    await state?.provider?.forceFlush();
  } catch {
    /* observability must never surface its own failures */
  }
}

/**
 * Test hook: drop the provider and everything it recorded so the next
 * `getTracer()` re-reads the environment. Never called by production code.
 */
export function resetTracingForTests(): void {
  try {
    void state?.provider?.shutdown();
  } catch {
    /* ignore */
  }
  state = undefined;
}
