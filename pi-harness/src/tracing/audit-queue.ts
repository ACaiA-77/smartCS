/**
 * Bounded audit queue + background dispatcher (plan v2 §6.9, phase6-design §3).
 *
 *   pi.on("tool_call"/"tool_result") → AuditQueue.push (sync, bounded)
 *        → AuditDispatcher (≤50 per batch, ≤1s interval)
 *        → POST /internal/audit → MySQL audit_event
 *
 * Semantics are deliberately best-effort, and this is the difference from the
 * memory outbox: a memory loss is unacceptable, so its queue is durable on the
 * receipt; an audit record may be lost at the tail (process crash, overflow)
 * but must never be distorted or duplicated once stored (event_id is the
 * idempotency key at the database). Overflow is therefore DROP + COUNT, never
 * back-pressure onto the hook, never unbounded growth.
 *
 * The hook path does exactly one thing here: `push()`. It never awaits the
 * dispatcher, the network or MySQL — that is the P6-3/P6-4 red line.
 */

import { randomUUID } from "node:crypto";
import type { PythonInternalClient, AuditEventPayload } from "../business/python-client.js";
import type { TurnIdentity } from "../business/turn-context.js";

export interface AuditEvent {
  eventId: string;
  kind: "tool_call" | "tool_result";
  toolName: string;
  toolCallId?: string;
  operationId?: string;
  traceId?: string;
  occurredAt: string;
  /** Program-only detail. Truncated by the dispatcher before it hits the wire. */
  payload?: unknown;
}

/** The turn identity an event belongs to; captured when the event is pushed. */
export interface AuditEventRoute {
  accountId: number;
  businessUserId: string;
  sessionId: string;
  clientRequestId: string;
  traceparent?: string;
}

export interface RoutedAuditEvent extends AuditEvent {
  route: AuditEventRoute;
}

/** Server-side cap (internal_api/audit.py); anything larger is refused there. */
export const MAX_AUDIT_PAYLOAD_BYTES = 16 * 1024;
const TRUNCATED_PREVIEW_CHARS = 2_048;

export class AuditQueue {
  private readonly items: RoutedAuditEvent[] = [];
  private droppedCount = 0;
  private enqueuedCount = 0;

  constructor(private readonly capacity = 1_000) {
    if (!Number.isInteger(capacity) || capacity <= 0) throw new Error("audit queue capacity must be positive");
  }

  /**
   * Never throws, never blocks: overflow is dropped and counted.
   *
   * The NEWEST event is the one discarded, so the queue always holds a
   * contiguous prefix of the burst — the audit trail may lose its tail, never
   * its beginning, and never anything in the middle.
   */
  push(event: AuditEvent, route: AuditEventRoute | undefined): boolean {
    if (!route) {
      // A tool ran outside a request, so there is no identity to authenticate
      // the delivery with. Dropping is the only honest option.
      this.droppedCount += 1;
      return false;
    }
    if (this.items.length >= this.capacity) {
      this.droppedCount += 1;
      return false;
    }
    this.items.push({ ...event, route });
    this.enqueuedCount += 1;
    return true;
  }

  /** Remove and return up to `max` events, oldest first. */
  drain(max: number): RoutedAuditEvent[] {
    return this.items.splice(0, Math.max(0, max));
  }

  get size(): number {
    return this.items.length;
  }

  get dropped(): number {
    return this.droppedCount;
  }

  get enqueued(): number {
    return this.enqueuedCount;
  }

  /** Test/diagnostic hook. */
  clear(): void {
    this.items.length = 0;
  }
}

export interface AuditDispatchSummary {
  drained: number;
  delivered: number;
  batches: number;
  failed: number;
  unroutable: number;
  /** Rows the runtime reported as already present (idempotent re-send). */
  duplicates: number;
}

export interface AuditDispatcherDeps {
  client: PythonInternalClient;
  queue: AuditQueue;
  batchSize?: number;
  intervalMs?: number;
  onError?: (error: unknown) => void;
}

function truncatePayload(payload: unknown): unknown {
  if (payload === undefined) return null;
  let encoded: string;
  try {
    encoded = JSON.stringify(payload) ?? "null";
  } catch {
    return { truncated: true, reason: "unserialisable" };
  }
  if (encoded.length <= MAX_AUDIT_PAYLOAD_BYTES) {
    try {
      return JSON.parse(encoded);
    } catch {
      return { truncated: true, reason: "unserialisable" };
    }
  }
  return {
    truncated: true,
    bytes: encoded.length,
    preview: encoded.slice(0, TRUNCATED_PREVIEW_CHARS),
  };
}

function toWireEvent(event: RoutedAuditEvent): AuditEventPayload {
  // `occurred_at` rides inside the payload: the design's table has no such
  // column, and created_at is the server's arrival clock, not the hook's.
  const body =
    event.payload && typeof event.payload === "object" && !Array.isArray(event.payload)
      ? { occurred_at: event.occurredAt, ...(event.payload as Record<string, unknown>) }
      : { occurred_at: event.occurredAt, value: event.payload ?? null };
  return {
    event_id: event.eventId,
    kind: event.kind,
    tool_name: event.toolName,
    tool_call_id: event.toolCallId ?? null,
    operation_id: event.operationId ?? null,
    trace_id: event.traceId ?? null,
    payload: truncatePayload(body),
  };
}

/**
 * Drains the queue off the request path. One bounded pass is exposed so tests
 * can drive delivery deterministically instead of sleeping on a timer.
 */
export class AuditDispatcher {
  private timer: NodeJS.Timeout | undefined;
  private inFlight = false;

  constructor(private readonly deps: AuditDispatcherDeps) {}

  get running(): boolean {
    return this.inFlight;
  }

  async flushOnce(): Promise<AuditDispatchSummary> {
    const summary: AuditDispatchSummary = {
      drained: 0,
      delivered: 0,
      batches: 0,
      failed: 0,
      unroutable: 0,
      duplicates: 0,
    };
    const batch = this.deps.queue.drain(this.deps.batchSize ?? 50);
    summary.drained = batch.length;
    if (batch.length === 0) return summary;

    const groups = new Map<string, RoutedAuditEvent[]>();
    for (const event of batch) {
      const key = `${event.route.sessionId}\u0000${event.route.clientRequestId}`;
      const group = groups.get(key);
      if (group) group.push(event);
      else groups.set(key, [event]);
    }

    for (const group of groups.values()) {
      const route = group[0]!.route;
      const identity: TurnIdentity = {
        accountId: route.accountId,
        businessUserId: route.businessUserId,
        sessionId: route.sessionId,
        clientRequestId: route.clientRequestId,
        ...(route.traceparent ? { traceparent: route.traceparent } : {}),
      };
      summary.batches += 1;
      try {
        const result = await this.deps.client.postAudit({
          identity,
          events: group.map(toWireEvent),
        });
        summary.delivered += group.length;
        summary.duplicates += result.duplicates;
      } catch (error) {
        summary.failed += group.length;
        this.deps.onError?.(error);
      }
    }
    return summary;
  }

  start(): void {
    if (this.timer) return;
    const interval = this.deps.intervalMs ?? 1_000;
    this.timer = setInterval(() => {
      if (this.inFlight) return; // never overlap passes
      this.inFlight = true;
      void this.flushOnce()
        .catch((error) => this.deps.onError?.(error))
        .finally(() => {
          this.inFlight = false;
        });
    }, interval);
    this.timer.unref?.();
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = undefined;
  }
}

/** Convenience for the hook path: build the event id + timestamp in one place. */
export function newAuditEvent(input: Omit<AuditEvent, "eventId" | "occurredAt">): AuditEvent {
  return { ...input, eventId: randomUUID(), occurredAt: new Date().toISOString() };
}
