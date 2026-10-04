/**
 * Harness-aware history projector (plan v2 §6.8, design §7).
 *
 * Red line: the UI history is NOT the model-context projection. Compaction is
 * "show the model less", not "delete the user's record" — so this projector
 * walks the *active branch* of the raw entry tree and keeps every user and
 * assistant message, skipping compaction summaries entirely.
 *
 * Context edits (SDK `ContextEditEntry`) ARE applied, because they represent a
 * deliberate replacement/omission of an earlier message's content. That is
 * exactly the semantics v2 §6.8 asks for, and Phase 0 D11 confirmed
 * `buildSessionProjection()` implements it while `getEntries()` keeps the
 * original text.
 */

import type { SessionMessageEntry, SessionEntry } from "@earendil-works/pi-coding-agent";
import { buildSessionProjection } from "@earendil-works/pi-coding-agent";

export interface HistoryMessage {
  role: "user" | "assistant";
  content: string;
  created_at: string;
}

function extractText(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((block) =>
      block && typeof block === "object" && (block as { type?: string }).type === "text"
        ? String((block as { text?: unknown }).text ?? "")
        : "",
    )
    .join("");
}

/**
 * Project the active branch into the stable public DTO
 * ({role, content, created_at}) that the web workbench already consumes.
 */
export function projectHistory(entries: SessionEntry[]): HistoryMessage[] {
  const { entries: projected } = buildSessionProjection(entries);
  const messages: HistoryMessage[] = [];

  for (const item of projected) {
    const source = item.sourceEntry as SessionMessageEntry;
    if (source.type !== "message") continue;
    const role = (source.message as { role?: string }).role;
    if (role !== "user" && role !== "assistant") continue;

    // A context edit replaces content but keeps the entry's identity/timestamp.
    const text = item.messages
      .filter((m) => (m as { role?: string }).role === role)
      .map((m) => extractText((m as { content?: unknown }).content))
      .join("");
    if (!text.trim()) continue;

    messages.push({ role, content: text, created_at: source.timestamp });
  }
  return messages;
}
