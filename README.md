# Personal Copilot · 受控个人工作台

基于 **Pi Harness（Node 22 / TypeScript / Pi SDK）+ Python Runtime（FastAPI）** 双层受控架构构建的 Personal Copilot 工作台，整合 **Obsidian LLM Wiki**、**轻量 Ontology 图谱**、**163 / Gmail 邮箱**、**GitHub**、**飞书** 与 **混合 RAG 检索**。

```text
Browser / Feishu Bot
        ↓
api.main (FastAPI) ── Auth (HttpOnly JWT) & Session Ownership
        ↓
pi-harness/ (Agent Harness, Node/TS)       ← 决定「想做什么」
        ↓  内部 HTTP + 每轮短期 Service JWT（身份信封）
Python Runtime /internal/* (FastAPI)       ← 决定「是否允许发生」
   ├─ /internal/auth/verify                身份解析（account → business_user）
   ├─ /internal/context/turn-snapshot      每轮确定性上下文（待确认操作 + 长期记忆摘要）
   ├─ /internal/tools/execute              READ / 本地 WRITE / 两段式外部 WRITE
   ├─ /internal/compliance/review          PII 掩码与合规审查
   ├─ /internal/memory/enqueue             长期记忆 Outbox 消费端
   └─ /internal/operation_status           ExecutionLedger 幂等状态裁决
```

**核心边界规则（由构造保证）**：
- Harness 只负责 Agent 循环、技能披露、SSE 状态推送与会话转录，不持久化业务状态、不自铸身份。
- 低风险本地写入（Obsidian 日记追加、笔记提案、Wiki 编译、Ontology 写入、邮件草稿）经同轮确定性同意判定后由 `ExecutionLedger` 幂等执行。
- 高风险外部副作用（发邮件、创建 GitHub Issue、发表评论、发送飞书消息）强制走两段式 **准备（`copilot_action_prepare` 冻结 `pending_action`）→ 人工明确确认 → 执行（`copilot_action_confirm`）**；取消待确认操作由运行时直接将 `pending_action` 置为 `cancelled`，不经过模型。

相关设计文档见 [docs/personal_copilot_plan.md](docs/personal_copilot_plan.md)，Harness 侧说明见 [pi-harness/README.md](pi-harness/README.md)。

---

## 技术栈

| 组件 | 技术 |
|------|------|
| Agent 运行时 | `pi-harness`（Node 22 / TypeScript / Pi SDK） |
| HTTP 运行时 | FastAPI + Uvicorn |
| 知识引擎 | `knowledge_engine/`（沙箱化 Obsidian Vault + SQLite Ontology + WikiCompiler） |
| 外部连接器 | `connectors/`（163/Gmail IMAP+SMTP、GitHub REST、飞书 OpenAPI） |
| 混合检索 | FAISS + BM25（`jieba`）+ RRF + Cross-Encoder |
| 持久化与账本 | MySQL（用户/会话/运行回执/待确认动作/记忆 Outbox）+ SQLite（`COPILOT_DB_PATH` 执行账本与审批） |
| 可观测性 | OpenTelemetry + `/metrics` / `/ready` |

---

## 快速开始

### 1. 安装依赖与配置环境变量

```powershell
pip install -r requirements.txt
pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

编辑 `.env`，配置 MySQL、JWT 密钥、模型凭据与 Copilot 工作区变量：

- `COPILOT_DB_PATH=./data/copilot.db`（执行账本与审批存储）
- `AUTH_JWT_SECRET`、`INTERNAL_SERVICE_JWT_SECRET`（至少 32 字节随机密钥）
- 可选连接器（未完整配置自动保持未挂载）：
  - `OBSIDIAN_VAULT_PATH=./data/obsidian_vault`（受管写区）、`OBSIDIAN_SOURCE_VAULT_PATH`（可选只读源 Vault，从不初始化或写入）、`OBSIDIAN_INDEX_PATH=./vector_store/obsidian_index`（独立个人本地 RAG 索引）、`COPILOT_ONTOLOGY_DB_PATH=./data/ontology.sqlite`
  - `GITHUB_TOKEN`
  - `FEISHU_APP_ID`、`FEISHU_APP_SECRET`（回调卡片取消还需 `FEISHU_VERIFICATION_TOKEN` / `FEISHU_ENCRYPT_KEY` 与 `FEISHU_OWNER_OPEN_ID` + `FEISHU_CHAT_BUSINESS_USER_ID`）
  - `MAIL_163_USERNAME` / `MAIL_163_PASSWORD`、`GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` / `GMAIL_REFRESH_TOKEN`

### 2. 初始化登录账号并启动服务

```powershell
# 创建本地工作台账号
python -m scripts.init_demo_auth_user --username copilot --business-user-id user_001

# 启动 Python Runtime（默认端口 8000）
python -m api.main

# 另一终端启动 Pi Harness（默认端口 8971）
cd pi-harness && npm ci && npm run dev
```

启动后访问 `http://127.0.0.1:8000/` 进入 Personal Copilot 工作台，Swagger UI 位于 `http://127.0.0.1:8000/docs`。

---

## 项目结构

```text
python-impl/
├── api/                   # FastAPI 入口、Copilot 依赖装配、Web 工作台接口、飞书 Bot Webhook
├── auth/                  # HttpOnly Cookie JWT 签发与用户上下文鉴权
├── connectors/            # 163/Gmail、GitHub、飞书异步客户端
├── knowledge_engine/      # Obsidian Vault 沙箱、SQLite Ontology、WikiCompiler
├── internal_api/          # 供 pi-harness 调用的内部通道（Service JWT）与 MCP 网关
├── memory/                # 长期记忆（MySQL Outbox + FAISS 向量库）
├── migrations/            # MySQL 增量迁移脚本（001..005）
├── pi-harness/            # Node/TS Agent Harness（prompt、copilot tools、skills-copilot、SSE）
├── platform_db/           # MySQL 连接与平台仓储
├── policies/              # 确定性合规审查、PII 掩码与用户同意判定
├── rag/                   # 混合检索（Dense + BM25 + RRF + Cross-Encoder）
├── scripts/               # 就绪检查、RAG 索引构建/评测、Obsidian 入库与账号初始化
├── tool_runtime/          # 受控工具执行器、ExecutionLedger 幂等账本与崩溃恢复
├── tracing/               # OpenTelemetry 与运行时指标
├── web/                   # Personal Copilot Web 工作台前端（HTML / CSS / JS）
├── benchmarks/            # RAG 评测数据集与 qrels
└── tests/                 # pytest 测试套件
```

---

## RAG 管线与索引构建

通用混合检索位于 `rag/`（支持 `agent_engineering` 语料与 Obsidian Vault 入库）：

```powershell
# 导入受管工作区与只读源 Vault 到独立个人 Obsidian 向量库（仅允许本地嵌入后端，保留共享索引且不暴露给共享 MCP 服务）
python -m scripts.ingest_obsidian_vault --vault-dir ./data/obsidian_vault --source-vault-dir <read-only-vault-path> --index-path ./vector_store/obsidian_index --embedding-backend local

# 构建双域/单域混合检索索引（默认 hash backend 离线验证；生产请指定 --embedding-backend local）
python -m scripts.build_rag_indexes --domain agent_engineering --embedding-backend local --embedding-model BAAI/bge-m3 --output-dir artifacts/rag_jieba/production_indexes
python -m scripts.build_global_sparse --artifact-root artifacts/rag_jieba/production_indexes
```

---

## 测试与就绪检查

```powershell
# 仓库就绪与边界检查
python -m scripts.check_repository_readiness

# Python 测试（含 MySQL 集成与跨服务 E2E）
python -m pytest -q -rs --junitxml=test-results/python.xml
python -m scripts.check_ci_test_results test-results/python.xml

# Pi Harness 类型检查与测试
cd pi-harness
npm run typecheck
npm run test:ci
npm test
```
