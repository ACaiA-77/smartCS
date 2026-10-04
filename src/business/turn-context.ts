/**
 * Per-turn identity for tool shells.
 *
 * Tool definitions are registered once, when the AgentSession is created, but
 * every tool call needs the identity of the *current* request. Rather than
 * re-registering tools per request, the pipeline publishes the verified
 * identity into a per-session holder before `prompt()` and clears it after.
 *
 * Safety: SessionRegistry guarantees at most one in-flight request per session,
 * so a single holder per session can never serve two requests at once. Reading
 * an empty holder means a tool ran outside a request, which is a programming
 * error and is refused rather than guessed.
 */

export interface TurnIdentity {
  accountId: number;
  businessUserId: string;
  sessionId: string;
  clientRequestId: string;
  /**
   * Phase 6 (plan v2 §6.9): the W3C context of the turn span. Every internal
   * HTTP call made for this turn carries it as a `traceparent` header, which is
   * what puts the Python server span in the same trace as the TS turn span.
   */
  traceparent?: string;
  /** The `agent_run_receipt` row id — the durable name of this request. */
  agentRunId?: string;
}

export class TurnContext {
  private current: TurnIdentity | undefined;

  set(identity: TurnIdentity): void {
    this.current = identity;
  }

  clear(): void {
    this.current = undefined;
  }

  /** Throws when no request is in flight — never falls back to a default. */
  require(): TurnIdentity {
    if (!this.current) {
      throw new Error("tool executed outside of a chat request");
    }
    return this.current;
  }

  peek(): TurnIdentity | undefined {
    return this.current;
  }
}
