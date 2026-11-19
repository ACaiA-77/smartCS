/**
 * Streaming primitives for Phase 0.
 *
 * Plan v2 §6.5 — Compliance-First Streaming:
 *  - the realtime channel carries only DETERMINISTIC status derived from tool
 *    name / runtime state; model narration must never leak as status;
 *  - the final answer is buffered, reviewed, and only sent after settle;
 *  - eligible final = assistant message_end with a normal stopReason and no
 *    toolCall, latest wins.
 */

export type StreamFrame =
  | { type: "status"; text: string; at: number }
  | { type: "final"; text: string; at: number }
  | { type: "done"; at: number; reason: "settled" | "aborted" | "error" };

/** Deterministic tool→status mapping. No model text is involved. */
const STATUS_BY_TOOL: Record<string, string> = {
  knowledge_search: "正在检索知识库",
  vault_search: "正在检索 Obsidian 笔记",
  vault_read_note: "正在读取笔记内容",
  vault_backlinks: "正在查询笔记反链",
  wiki_lint: "正在巡检 Wiki 链接",
  onto_query: "正在查询本体图谱",
  mail_search: "正在检索邮箱",
  mail_read_thread: "正在读取邮件会话",
  gh_list_notifications: "正在获取 GitHub 通知",
  gh_get_issue_or_pr: "正在读取 Issue / PR",
  gh_search_code: "正在搜索 GitHub 代码",
  feishu_list_chats: "正在获取飞书会话",
  feishu_read_messages: "正在读取飞书消息",
  feishu_read_doc: "正在读取飞书文档",
  vault_append_daily: "正在追加今日日记",
  vault_propose_edit: "正在生成笔记修改提案",
  wiki_compile_doc: "正在编译 Wiki 页面",
  onto_upsert: "正在写入本体实体",
  mail_save_draft: "正在保存邮件草稿",
  copilot_action_prepare: "正在准备待确认操作",
  copilot_action_confirm: "正在执行已确认的外部操作",
  skill_load: "正在加载工作台技能",
};

export function statusForTool(toolName: string): string {
  return STATUS_BY_TOOL[toolName] ?? "正在处理你的请求";
}

export function extractMessageText(message: unknown): string {
  const content = (message as { content?: unknown })?.content;
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
 * An eligible final candidate is an assistant message that finished normally
 * and contains no tool call (a tool-call narration is not a final answer).
 */
export function isEligibleFinal(message: unknown): boolean {
  const m = message as { role?: string; stopReason?: string; content?: unknown };
  if (m?.role !== "assistant") return false;
  if (m.stopReason !== "stop") return false;
  if (Array.isArray(m.content) && m.content.some((b) => (b as { type?: string })?.type === "toolCall")) {
    return false;
  }
  return extractMessageText(message).trim().length > 0;
}

/**
 * Holds the latest eligible final. Plan v2 §6.5: "latest wins"; when no legal
 * candidate exists the caller must emit a deterministic safe fallback.
 */
export class RunOutputBuffer {
  private latest: string | undefined;

  record(message: unknown): boolean {
    if (!isEligibleFinal(message)) return false;
    this.latest = extractMessageText(message);
    return true;
  }

  peek(): string | undefined {
    return this.latest;
  }

  take(): string | undefined {
    const value = this.latest;
    this.latest = undefined;
    return value;
  }

  reset(): void {
    this.latest = undefined;
  }
}

export const SAFE_FALLBACK_FINAL = "抱歉，暂时无法完成这次处理，请稍后再试或换个方式描述需求。";

/** Minimal SSE writer: one `event:`/`data:` pair per frame. */
export function formatSseFrame(frame: StreamFrame): string {
  return `event: ${frame.type}\ndata: ${JSON.stringify(frame)}\n\n`;
}
