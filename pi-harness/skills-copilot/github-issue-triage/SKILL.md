---
name: github-issue-triage
description: GitHub 通知、Issue 与 PR 分诊。当需要查看未读通知、理解某个 Issue/PR、搜索相关代码，或准备建 Issue、发评论时使用。
---

# GitHub Issue 分诊

## 先读
1. 用 `gh_list_notifications` 拉取通知；必要时把 `participating=true` 限定到自己参与过的条目。
2. 对每条重要通知用 `gh_get_issue_or_pr` 读取标题、正文、状态与评论。
3. 需要判断影响面时用 `gh_search_code` 搜索相关代码，可用 `repo="owner/name"` 限定仓库。
4. 归纳时区分：需要我回复的、等待别人的、可以直接关闭/忽略的。

## 写入必须两段式
- 建 Issue（`gh_create_issue`）与发评论（`gh_post_comment`）都是外部副作用。
- 先调用 `copilot_action_prepare`：
  - 建 Issue：`tool="gh_create_issue"`，`arguments` 含 `owner`、`repo`、`title`，可选 `body`、`labels`；
  - 发评论：`tool="gh_post_comment"`，`arguments` 含 `owner`、`repo`、`number`、`body`。
- 把预览展示给用户，**等待用户明确回复确认**。
- 用户确认后才调用 `copilot_action_confirm`，原样传入 `pending_action_id`。
- 不要在准备阶段声称"已创建"或"已评论"；结果以工具返回为准。
