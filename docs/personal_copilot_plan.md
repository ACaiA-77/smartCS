# SmartCS → 个人受控 Copilot 改造计划（`feat/personal-copilot`）

> 目标：保留 SmartCS 最核心的 **「模型提案、运行时确定性授权、崩溃幂等账本」** 安全底座，将 Demo 级的电商业务外壳替换为真实个人工作台：接入 **Obsidian（LLM Wiki + RAG 兜底）+ 轻量 Ontology + 163 邮箱 + Gmail + GitHub + 飞书**。

## 架构总览

- **不变的底座（安全红线）**：
  - `internal_api/write_authorization.py` + `policies/consent.py`：写操作永远不能由模型参数 `confirmed=True` 授权，必须经运行时确定性校验与 `pending_action` 冻结快照。
  - `tool_runtime/execution_ledger.py`：写副作用具备幂等去重与崩溃恢复能力，避免网络重试导致重复发送邮件/消息或重复建 Issue。
  - `pi-harness/`：Agent 循环、技能按需加载（`skill_load`）、合规审计钩子与 SSE 流式输出。
- **新增业务与知识层**：
  1. `knowledge_engine/`：Obsidian Vault 沙箱读写、Karpathy 式 LLM Wiki 编译/Lint、SQLite 轻量 Ontology（实体 + 跨平台别名消歧 + 关系图谱）。
  2. `connectors/`：统一邮件抽象（163 IMAP/SMTP + Gmail OAuth2 API）、GitHub REST 连接器、飞书 OpenAPI 连接器。
  3. `tool_runtime/` & `internal_api/`：注册新工具，接入两段式写确认与账本幂等保护，打通 Obsidian `raw/`/`notes/` 到 `rag/retriever.py` 的兜底检索。
  4. `pi-harness/` & `api/`：Copilot 人设 Prompt、SOP 技能包、工具面切换、飞书长连接/卡片审批入口。

---

## 分阶段执行里程碑

### Milestone 1：Obsidian LLM Wiki + 轻量 Ontology 知识引擎（`knowledge_engine/`）
- **新增模块**：
  - `knowledge_engine/__init__.py`
  - `knowledge_engine/vault.py`：`ObsidianVault`
    - 严格路径校验：所有读写必须 `Path.resolve()` 落在 `vault_root` 内，拒绝 `..` 或符号链接逃逸。
    - 目录分区权限：`notes/` 只读（外部修改仅走 `propose_note_edit` 提案）、`raw/` 只追加归档、`wiki/` 编译区（含 `wiki/index.md` 与 `wiki/log.md`）。
    - 能力：`search()`、`read_note()`、`backlinks()`（解析 `[[wikilink]]` 与 `[[target|alias]]`）、`append_daily()`、`write_wiki_page()`、`propose_note_edit()`。
  - `knowledge_engine/ontology.py`：`OntologyStore`（SQLite）
    - 表结构：`entities`、`entity_aliases`、`relations`。
    - 支持 `Person`、`Project`、`RepoIssue`、`MailThread`、`NotePage`、`ActionItem` 六类实体及跨平台别名映射（邮箱 / GitHub handle / 飞书 `open_id` / Obsidian 人名页）。
    - 查询接口：`upsert_entity()`、`link_alias()`、`resolve_by_alias()`、`add_relation()`、`get_neighbors()`、`query_entities()`。
  - `knowledge_engine/wiki_compiler.py`：`WikiCompiler`
    - 增量编译 `ingest_document()`（写 `raw/` 快照 + 更新 `wiki/` 条目 + 更新 `wiki/index.md` 与 `wiki/log.md`）。
    - 健康检查 `lint_wiki()`（检测断链 `[[missing]]`、孤儿页面、缺失索引条目）。
- **测试验收**：`tests/test_knowledge_engine.py`（纯离线 `tmp_path` + SQLite，覆盖路径逃逸拦截、分区权限、双链解析、跨平台身份消歧、Wiki Lint）。

### Milestone 2：五大外部连接器（`connectors/`）
- `connectors/base.py`：统一配置加载、超时/重试约定、幂等 payload hash 辅助。
- `connectors/mail.py`：统一 `MailProvider` 接口，`ImapSmtpMailClient`（163 邮箱 SSL 993/465 + 客户端授权码）与 `GmailApiClient`（OAuth2 `refresh_token` + Gmail REST API），提供 `search_messages`、`read_thread`、`save_draft`、`send_message`。
- `connectors/github.py`：`GitHubClient`（Fine-grained PAT + `httpx`），提供 `list_notifications`、`get_issue_or_pr`、`search_code`、`create_issue`、`post_comment`。
- `connectors/feishu.py`：`FeishuClient`（`tenant_access_token` 自动刷新 + 卡片/消息/文档 API），提供 `list_chats`、`read_messages`、`read_doc`、`send_message`。
- **测试验收**：`tests/test_connectors_mail.py`、`tests/test_connectors_github.py`、`tests/test_connectors_feishu.py`（基于 `httpx.MockTransport` 与伪造 IMAP/SMTP，全离线可测）。

### Milestone 3：工具注册、两段式写授权与 RAG 接入
- `tool_runtime/mcp_server.py`：新增 `vault_*`、`wiki_*`、`onto_*`、`mail_*`、`gh_*`、`feishu_*` 工具定义与元数据（读写分级、`requires_confirmation=True`）。
- `internal_api/tools.py` & `internal_api/write_authorization.py` & `policies/consent.py`：将两段式 `pending_action` 冻结快照与幂等 `ExecutionLedger` 扩展至 `mail_send`、`gh_create_issue`、`gh_post_comment`、`feishu_send_message`、`vault_propose_edit`。
- `scripts/ingest_obsidian_vault.py`：扫描 Obsidian `raw/` + `notes/` 生成 `rag/` 混合检索索引，作为 Wiki/Ontology 未命中时的兜底检索。
- **测试验收**：补充授权拒签、参数篡改拦截、崩溃幂等去重集成测试。

### Milestone 4：`pi-harness` Copilot 改造与飞书交互入口
- `pi-harness/src/agent/prompt/`：新增个人 Copilot System Prompt（严格对应挂载工具面）。
- `pi-harness/src/agent/tools/`：暴露 Copilot Read/Write 工具壳。
- `pi-harness/skills/`：启用 `SMARTCS_SKILLS=on`，编写 `obsidian-wiki-compile`、`mail-triage-reply`、`github-issue-triage`、`daily-briefing` 技能。
- `api/`：新增飞书事件回调与交互卡片“确认/取消”审批端点。
- **测试验收**：Vitest offline suite + Python pytest 回归全绿。
