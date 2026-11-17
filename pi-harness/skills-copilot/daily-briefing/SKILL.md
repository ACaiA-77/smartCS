---
name: daily-briefing
description: 跨来源每日简报。当用户需要"今天有什么需要处理"的汇总，或要把当日关键事项落到 Obsidian 日记时使用。
---

# 每日简报

## 汇总来源（可用的工具才调用）
1. `vault_search`：检索 wiki / daily 中的今日计划与未完成事项。
2. `onto_query`：查询 ActionItem、Project、Person 等实体，补齐上下文。
3. `mail_search`：按需检索 163 与 Gmail 中今天的未读或待回复邮件。
4. `gh_list_notifications`：查看未读 GitHub 通知与需要回复的 PR/Issue。
5. `feishu_list_chats` 与 `feishu_read_messages`：查看相关群聊或单聊的关键消息。

## 组织简报
- 按"需要今天处理 / 等待他人 / 仅供参考"三段输出，每条给出可追溯的来源。
- 明确区分事实与推断；没有工具证据的内容标注为"未确认"。

## 落到日记
- 用 `vault_append_daily` 把简报要点追加到当日日记（`daily/YYYY-MM-DD.md`），`entry` 写 Markdown。
- 只追加，不覆盖；需要修改既有笔记时走 `vault_propose_edit` 提案。
- 若需要对外发送（例如把简报发到飞书），先 `copilot_action_prepare`，等待用户明确确认后再 `copilot_action_confirm`。
