---
name: mail-triage-reply
description: 163 与 Gmail 邮件统一分诊与回复。当需要梳理未读邮件、归纳往来、起草回复，或准备发送邮件时使用。
---

# 邮件分诊与回复

## 统一分诊
1. 用 `mail_search` 分别检索 `account="163"` 与 `account="gmail"`（只查已配置的账号）。
2. 对需要上下文的邮件用 `mail_read_thread` 读完整会话，不要只看单封。
3. 输出分诊结果：需要今天处理的、可以稍后处理的、只需归档的。

## 起草先于发送
- 任何回复都先用 `mail_save_draft` 保存草稿：`account`、`to`、`subject`、`body` 必填，`cc` 与 `reply_to_message_id` 按需。
- 草稿是本地副作用，可以直接创建；创建后把主题、收件人与正文摘要展示给用户。

## 外部发送必须两段式
- 真正发送邮件（`mail_send`）属于外部副作用，必须：
  1. 调用 `copilot_action_prepare`，`tool="mail_send"`，`arguments` 放齐 `account`、`to`、`subject`、`body`（可含 `cc`、`reply_to_message_id`）；
  2. 把冻结的预览展示给用户，**等待用户明确回复确认**；
  3. 用户确认后才调用 `copilot_action_confirm`，原样传入返回的 `pending_action_id`。
- 不要在准备阶段就声称"已发送"；不要替用户确认。
- 身份、幂等键、确认标记都是运行时字段，不要写进 `arguments`。
