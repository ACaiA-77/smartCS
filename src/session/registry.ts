/**
 * SessionRegistry — the single writer guarantee for a session (plan v2 §6.2).
 *
 * Phase 0 A5 measured that Pi's SessionManager takes NO cross-process lock, and
 * that two writers on one session id either split into two files or crash with
 * EEXIST. This registry is therefore the ONLY thing preventing concurrent
 * writers: one in-process mutex per session, every HTTP request funnelled
 * through it, `inFlight` never above 1 (F8 assertion point).
 *
 * idle eviction disposes and forgets; it never "saves" — durability lives in
 * the Pi append (plan v2 §5.2).
 */

import type { AgentSession } from "@earendil-works/pi-coding-agent";
import type { TurnContext } from "../business/turn-context.js";
import type { SnapshotHolder } from "../agent/extensions/context-injection.js";

export class SessionBusyError extends Error {}
export class SessionQueueFullError extends Error {}

export interface SessionLease {
  sessionId: string;
  release(): void;
}

export interface ActiveSession {
  sessionId: string;
  session: AgentSession;
  lastUsedAt: number;
  inFlight: number;
}

export interface RegistryStats {
  active: number;
  queued: number;
  evicted: number;
}

export interface RegistryOptions {
  /** Max queued waiters per session before new arrivals are rejected. */
  maxQueuePerSession?: number;
  /** Default acquire timeout. */
  acquireTimeoutMs?: number;
}

interface Waiter {
  resolve: (release: () => void) => void;
  reject: (error: Error) => void;
  timer: NodeJS.Timeout;
}

/**
 * What the registry keeps per session. `turnContext` is the identity holder the
 * tool shells close over; the pipeline publishes the verified identity there
 * for the duration of one prompt.
 */
export interface SessionRuntime {
  session: AgentSession;
  origin: "opened" | "created";
  turnContext?: TurnContext;
  snapshotHolder?: SnapshotHolder;
}

interface Entry {
  sessionId: string;
  locked: boolean;
  queue: Waiter[];
  session?: AgentSession;
  turnContext?: TurnContext;
  snapshotHolder?: SnapshotHolder;
  origin: "opened" | "created" | "unknown";
  lastUsedAt: number;
  evictTimer?: NodeJS.Timeout;
  /** Resolvers waiting for the session to become fully idle (dispose path). */
  idleWaiters: Array<() => void>;
}

export class SessionRegistry {
  private readonly entries = new Map<string, Entry>();
  private readonly maxQueuePerSession: number;
  private readonly acquireTimeoutMs: number;
  private evicted = 0;

  constructor(
    private readonly createSession: (sessionId: string) => Promise<SessionRuntime>,
    options: RegistryOptions = {},
  ) {
    this.maxQueuePerSession = options.maxQueuePerSession ?? 64;
    this.acquireTimeoutMs = options.acquireTimeoutMs ?? 30_000;
  }

  private entry(sessionId: string): Entry {
    let entry = this.entries.get(sessionId);
    if (!entry) {
      entry = {
        sessionId,
        locked: false,
        queue: [],
        origin: "unknown",
        lastUsedAt: Date.now(),
        idleWaiters: [],
      };
      this.entries.set(sessionId, entry);
    }
    return entry;
  }

  /** Same-session requests are strictly serialised; the second one waits. */
  async acquire(sessionId: string, timeoutMs?: number): Promise<SessionLease> {
    const entry = this.entry(sessionId);
    if (entry.evictTimer) {
      clearTimeout(entry.evictTimer);
      entry.evictTimer = undefined;
    }

    if (!entry.locked) {
      entry.locked = true;
      entry.lastUsedAt = Date.now();
      return this.lease(entry);
    }
    if (entry.queue.length >= this.maxQueuePerSession) {
      throw new SessionQueueFullError(`session queue full: ${sessionId}`);
    }

    const limit = timeoutMs ?? this.acquireTimeoutMs;
    return await new Promise<SessionLease>((resolve, reject) => {
      const waiter: Waiter = {
        resolve: (release) => {
          entry.lastUsedAt = Date.now();
          clearTimeout(waiter.timer);
          resolve({
            sessionId,
            release: () => {
              release();
              entry.lastUsedAt = Date.now();
            },
          });
        },
        reject: (error) => {
          clearTimeout(waiter.timer);
          reject(error);
        },
        timer: setTimeout(() => {
          const index = entry.queue.indexOf(waiter);
          if (index >= 0) entry.queue.splice(index, 1);
          reject(new SessionBusyError(`timed out waiting for session ${sessionId}`));
        }, limit),
      };
      waiter.timer.unref?.();
      entry.queue.push(waiter);
    });
  }

  /** Non-blocking acquire: returns undefined when the session is busy. */
  tryAcquire(sessionId: string): SessionLease | undefined {
    const entry = this.entry(sessionId);
    if (entry.locked) return undefined;
    entry.locked = true;
    entry.lastUsedAt = Date.now();
    if (entry.evictTimer) {
      clearTimeout(entry.evictTimer);
      entry.evictTimer = undefined;
    }
    return this.lease(entry);
  }

  private lease(entry: Entry): SessionLease {
    let released = false;
    return {
      sessionId: entry.sessionId,
      release: () => {
        if (released) return;
        released = true;
        const next = entry.queue.shift();
        if (next) {
          // Hand the lock straight to the next waiter; stay locked.
          next.resolve(() => this.releaseTo(entry));
        } else {
          entry.locked = false;
          this.markIdle(entry);
        }
      },
    };
  }

  private releaseTo(entry: Entry): void {
    const next = entry.queue.shift();
    if (next) {
      next.resolve(() => this.releaseTo(entry));
    } else {
      entry.locked = false;
      this.markIdle(entry);
    }
  }

  private markIdle(entry: Entry): void {
    if (entry.locked || entry.queue.length) return;
    const waiters = entry.idleWaiters.splice(0, entry.idleWaiters.length);
    for (const resolve of waiters) resolve();
  }

  /** The live session for `sessionId`, created (先查后建) on first use. */
  async ensureSession(sessionId: string): Promise<SessionRuntime> {
    const entry = this.entry(sessionId);
    if (!entry.session) {
      const created = await this.createSession(sessionId);
      entry.session = created.session;
      entry.turnContext = created.turnContext;
      entry.snapshotHolder = created.snapshotHolder;
      entry.origin = created.origin;
    }
    return {
      session: entry.session,
      turnContext: entry.turnContext,
      snapshotHolder: entry.snapshotHolder,
      origin: entry.origin === "unknown" ? "created" : entry.origin,
    };
  }

  /** The live session for `sessionId` (design §4 signature), created on first use. */
  async session(sessionId: string): Promise<AgentSession> {
    return (await this.ensureSession(sessionId)).session;
  }

  /** How the current runtime obtained this session ("opened" | "created"). */
  originOf(sessionId: string): "opened" | "created" | "unknown" {
    return this.entries.get(sessionId)?.origin ?? "unknown";
  }

  get(sessionId: string): ActiveSession | undefined {
    const entry = this.entries.get(sessionId);
    if (!entry?.session) return undefined;
    return {
      sessionId,
      session: entry.session,
      lastUsedAt: entry.lastUsedAt,
      inFlight: entry.locked ? 1 : 0,
    };
  }

  /** Dispose and forget after `afterMs` idle. Never saves (plan v2 §5.2). */
  scheduleIdleEviction(sessionId: string, afterMs: number): void {
    const entry = this.entries.get(sessionId);
    if (!entry) return;
    if (entry.evictTimer) clearTimeout(entry.evictTimer);
    entry.evictTimer = setTimeout(() => {
      void this.evictIfIdle(sessionId);
    }, afterMs);
    entry.evictTimer.unref?.();
  }

  private async evictIfIdle(sessionId: string): Promise<void> {
    const entry = this.entries.get(sessionId);
    if (!entry) return;
    if (entry.locked || entry.queue.length) {
      // Busy again: retry later rather than dropping the session mid-run.
      this.scheduleIdleEviction(sessionId, 1_000);
      return;
    }
    entry.evictTimer = undefined;
    entry.session?.dispose();
    entry.session = undefined;
    this.entries.delete(sessionId);
    this.evicted += 1;
  }

  /** Wait until nothing is in flight, then run `fn` under the lock. */
  async withExclusive<T>(sessionId: string, fn: (lease: SessionLease) => Promise<T>): Promise<T> {
    const lease = await this.acquire(sessionId);
    try {
      return await fn(lease);
    } finally {
      lease.release();
    }
  }

  /** Dispose a session immediately (DELETE history). Caller must hold the lease. */
  disposeNow(sessionId: string): void {
    const entry = this.entries.get(sessionId);
    if (!entry) return;
    if (entry.evictTimer) clearTimeout(entry.evictTimer);
    entry.session?.dispose();
    entry.session = undefined;
    this.entries.delete(sessionId);
  }

  stats(): RegistryStats {
    let queued = 0;
    let active = 0;
    for (const entry of this.entries.values()) {
      queued += entry.queue.length;
      if (entry.locked) active += 1;
    }
    return { active, queued, evicted: this.evicted };
  }

  /** Test/diagnostic: is the session currently held? */
  isLocked(sessionId: string): boolean {
    return this.entries.get(sessionId)?.locked ?? false;
  }

  async shutdown(): Promise<void> {
    for (const entry of this.entries.values()) {
      if (entry.evictTimer) clearTimeout(entry.evictTimer);
      entry.session?.dispose();
      for (const waiter of entry.queue.splice(0)) {
        waiter.reject(new SessionBusyError("registry shutting down"));
      }
    }
    this.entries.clear();
  }
}
