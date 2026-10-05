/**
 * Compliance extension — the A2承重墙 probe.
 *
 * Plan v2 §6.5: an eligible final assistant message is reviewed inside the
 * `message_end` extension, which may return a REPLACEMENT message. If that API
 * does not exist, the compliance design in §6.5 does not hold and Phase 3 must
 * be redesigned (risk R7).
 *
 * Phase 0 deliberately keeps the policy trivial and deterministic (a rule
 * mask); the point is proving the replacement API and its persistence effect,
 * not implementing the real safety filter.
 */

import type { ExtensionAPI, ExtensionFactory } from "@earendil-works/pi-coding-agent";
import type { AgentMessage } from "@earendil-works/pi-agent-core";

export type ComplianceAction = "pass" | "sanitize" | "skip";

export interface ComplianceRecord {
  /** Entry id in the session file the extension observed. */
  action: ComplianceAction;
  role: string;
  /** Text the model produced. */
  originalText: string;
  /** Text after replacement (equals originalText on pass). */
  finalText: string;
  reason?: string;
}

export interface ComplianceOptions {
  /** Substrings that must never reach the user verbatim. */
  bannedPatterns?: RegExp[];
  /** Replacement text used when a banned pattern matched. */
  sanitizedText?: string;
  /**
   * Phase 3: the authoritative reviewer, normally
   * `PythonInternalClient.reviewCompliance`. When set it runs the runtime's own
   * rule mask (mandatory) plus the optional LLM stage, and its verdict decides.
   *
   * This DOES await the network inside `message_end`, which is deliberate and
   * specific to this hook: plan v2 §6.5 places the compliance review on the
   * critical path and accepts the latency as parity with the existing Python
   * pipeline. The "no network in pi.on" rule (plan v2 §6.6) governs the
   * snapshot/memory injection path, which is prefetched instead.
   */
  reviewer?: (text: string, signal?: AbortSignal) => Promise<
    { verdict: "pass" | "sanitize" | "fail"; replacement: string | null } | undefined
  >;
  /** Deterministic fallback emitted when the verdict is `fail`. */
  fallbackText?: string;
}

const DEFAULT_BANNED = [/\bAPI[_ ]?KEY\b/i, /sk-[A-Za-z0-9]{8,}/, /IGNORE ALL PREVIOUS/i];

function extractText(message: AgentMessage): string {
  const content = (message as { content?: unknown }).content;
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((block) => (block && typeof block === "object" && (block as { type?: string }).type === "text"
      ? String((block as { text?: unknown }).text ?? "")
      : ""))
    .join("");
}

function withText(message: AgentMessage, text: string): AgentMessage {
  const content = (message as { content?: unknown }).content;
  if (typeof content === "string") {
    return { ...message, content: text } as AgentMessage;
  }
  if (Array.isArray(content)) {
    const next = content.map((block) =>
      block && typeof block === "object" && (block as { type?: string }).type === "text"
        ? { ...block, text }
        : block,
    );
    return { ...message, content: next } as AgentMessage;
  }
  return message;
}

export interface ComplianceHook {
  records: ComplianceRecord[];
  /** Number of times the extension actually returned a replacement. */
  replacementCount: number;
}

/**
 * Build the compliance extension plus a handle used by tests to assert what
 * the hook observed. Only assistant messages are reviewed; a replacement always
 * keeps the original role (SDK requirement, see MessageEndEventResult).
 */
export function createComplianceExtension(options: ComplianceOptions = {}): {
  extension: ExtensionFactory;
  hook: ComplianceHook;
} {
  const banned = options.bannedPatterns ?? DEFAULT_BANNED;
  const sanitizedText = options.sanitizedText ?? "（该内容已被安全策略拦截，请重新描述你的问题。）";
  const hook: ComplianceHook = { records: [], replacementCount: 0 };

  const fallbackText = options.fallbackText ?? "抱歉，该回复未能通过合规检查，请稍后再试或转人工客服。";

  const extension: ExtensionFactory = (pi: ExtensionAPI) => {
    pi.on("message_end", async (event) => {
      const message = (event as { message: AgentMessage }).message;
      if ((message as { role?: string }).role !== "assistant") return undefined;

      if (options.reviewer) {
        const originalText = extractText(message);
        if (!originalText.trim()) {
          hook.records.push({ action: "skip", role: "assistant", originalText, finalText: originalText });
          return undefined;
        }
        const verdict = await options.reviewer(originalText);
        if (!verdict || verdict.verdict === "pass") {
          hook.records.push({ action: "pass", role: "assistant", originalText, finalText: originalText });
          return undefined;
        }
        const replacement = verdict.verdict === "fail" ? fallbackText : verdict.replacement ?? fallbackText;
        hook.replacementCount += 1;
        hook.records.push({
          action: "sanitize",
          role: "assistant",
          originalText,
          finalText: replacement,
          reason: verdict.verdict,
        });
        // Must keep the original role — MessageEndEventResult contract.
        return { message: withText(message, replacement) };
      }

      const originalText = extractText(message);
      const hit = banned.find((pattern) => pattern.test(originalText));
      if (!hit) {
        hook.records.push({ action: "pass", role: "assistant", originalText, finalText: originalText });
        return undefined;
      }

      hook.replacementCount += 1;
      hook.records.push({
        action: "sanitize",
        role: "assistant",
        originalText,
        finalText: sanitizedText,
        reason: String(hit),
      });
      // Replacement must keep the original role — MessageEndEventResult contract.
      return { message: withText(message, sanitizedText) };
    });
  };

  return { extension, hook };
}
