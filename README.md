# SmartCS — Business Runtime（业务运行时）

SmartCS 是一个**双层**智能客服系统，本仓库是其 Python 侧：**Business Runtime**。
Agent 运行时（Agent Harness）是 `../pi-harness`（Node 22 / TypeScript / Pi SDK）。

```text
        Agent Harness (pi-harness, TS/Pi)          ← 决定「想做什么」
                    ↓
        内部 HTTP + 每轮 Service JWT（身份信封）
                    ↓
        Business Runtime (python-impl, FastAPI)    ← 决定「是否允许发生」
```

**边界规则（由设计保证，不靠约定）**：Agent 可以提出工具调用；业务写操作是否真正执行，
由本仓库的确定性授权与执行层最终决定。Harness 不持久化业务状态、不自铸可信身份、
也不把模型 transcript 当作业务事实。

对外主链路的完整说明见 [docs/architecture.md](docs/architecture.md)，
边界规则见 [docs/runtime-boundaries.md](docs/runtime-boundaries.md)，
崩溃与恢复语义见 [docs/recovery.md](docs/recovery.md)。

## 当前迭代范围

本版保留认证、客户隔离、checkpoint 恢复和双域文本 RAG，增加 jieba 索引重建与可续跑的分词 A/B 评测工具。图片、音频和视频输入留待下一版本，本版不包含多模态功能。下方带日期的验收数据为历史记录，不代表当前 GitHub CI 或部署状态。

## Historical Migration Record

> 以下三节（Verified Local Status / Final RAG Runtime Closure / 用户登录与访问控制中的验收段落）
> 是**历史迁移记录**，保留原样以维持证据链。它们描述的是各阶段当时的验收结果，
> **不是当前架构入口**，也不与后续数字相加。当前架构请从
> [docs/architecture.md](docs/architecture.md) 读起；
> 各阶段报告位于 `../pi-harness/PHASEn_REPORT.md`，原始验收产物位于 `artifacts/`。

## Verified Local Status（Checkpoint 历史基线）

| 项目 | 当前记录 |
|------|---------|
| 全量 `pytest`（显式启用真实 MySQL） | 284 passed in 58.14s |
| checkpoint 目标测试（显式启用真实 MySQL） | 14 passed；隔离会话和业务 SQLite |
| 离线 Agent Eval | 14 / 14 scenarios passed |
| Node 重连回归 | 3 passed |
| 路由准确率 | 1.0 |
| 副作用安全率 | 1.0 |
| 故障收敛率 | 1.0 |
| 仓库就绪状态 | PASS / READY_TO_COMMIT |

以上是 2026-09-18 checkpoint 范围的历史结果，不代表后续 Auth 改造已通过。Auth 本轮结果与原始输出见 `artifacts/auth_20260919/`。本地确定性验证使用 Mock / Deterministic LLM 和隔离业务 Sandbox，不代表在线模型质量、真实流量、生产 SLA 或远程 CI。

## Final RAG Runtime Closure（2026-09-20）

RAG runtime isolation 已通过独立验收并冻结：生产 composition root 继续从环境加载双领域 artifact retriever；显式注入的 `LongTermMemory`、MCP 和 Eval 使用 `get_retriever(use_env=False)`，不受 `RAG_INDEX_ROOT` 污染。最终回归为 `367 passed, 18 skipped`，离线 Eval `14 / 14`，Node UI `15 / 15`；两种测试顺序和 API smoke 均通过，原始输出见 `artifacts/smartcs_final_20260920/`。双域 Retrieval Benchmark 的真实模型结果保存在 `artifacts/rag_round3/`：`hybrid_rerank` 的 global Top-10 为 Recall `0.825`、MRR `0.800`、nDCG `0.749`，wrong-domain rate `0.063`。这些是本地固定数据集结果，不代表线上质量或生产 SLA。

## 用户登录与访问控制

本轮本地验收：认证阶段全量 `351 passed in 71.72s`，Auth 专项 `63 passed in 28.67s`，checkpoint 专项 `14 passed in 21.34s`；随后 RAG runtime isolation 收尾使最终全量达到 `367 passed, 18 skipped`。Node 15 / 15、离线 Eval 14 / 14、真实 MySQL、JWT、HTTP 进程重启和 Chrome 双账号验收均通过。认证专项测试已包含在最终全量数量内，不重复累计；完整认证证据见 [执行回报](artifacts/auth_20260919/execution_report.md)。

浏览器通过 `POST /api/auth/login` 登录，服务端验证 Argon2 密码哈希并设置 HttpOnly JWT cookie。每次客户请求都校验签名、期限和发行者，再从 MySQL 读取账号状态及业务身份。前端不能指定 `user_id` 或 `business_user_id`。

MySQL 新增 `platform_user` 和 `conversation_session`，与已有 checkpoint 表使用同一实例。平台账号 ID 用于认证，会话按账号归属；业务身份映射到 SQLite 已存在的用户。订单、退款、工单和执行账本不迁移。

首次配置需设置随机的 `AUTH_JWT_SECRET`，不要使用示例值或提交密钥。HTTPS 部署设置 `AUTH_COOKIE_SECURE=true`；本地 HTTP 使用 false。默认同源，跨源开发仅接受 `CORS_ALLOWED_ORIGINS` 精确白名单。Cookie 写请求还校验 Origin。

本地账号通过受控脚本创建，不提供公开注册，也不允许网页任意关联业务用户：

```text
niu -c 'python -m scripts.init_demo_auth_user --username alice --business-user-id user_001'
```

密码通过安全交互提示输入；自动化可使用 `SMARTCS_DEMO_PASSWORD`，用后清除环境变量，不打印或提交。脚本核实 SQLite 业务用户存在后，才保存密码哈希。没有默认明文密码。

登录后，左侧是自己的会话，右侧是自己的订单。`POST /api/chat` 不带 session_id 时由服务端创建会话；网页先调用 `POST /api/sessions` 获得服务端 ID，再发送消息，便于断网后用原请求 ID 继续。未认证返回 401，非本人会话与订单不返回内容。

`/api/tools/call` 和 `/api/tools/execute` 仅向客户开放指定 READ 工具。WRITE 必须经聊天和原 `ToolExecutor` 策略；客户不能自行审批高风险操作。审批 HTTP 接口和含全局工具明细的 `/api/metrics` 不对客户开放。

JWT 有效期 30 分钟。登出清除浏览器 cookie，不提供 refresh-token rotation 或单个已复制 JWT 的撤销列表；令牌到期或禁用账号后不能继续使用。公开部署前还需登录限流、HTTPS 与独立的安全运维验收。本轮不声称企业 IAM、SSO、完整 RBAC 或生产零信任。

CLI 登录和 cookie 写请求需显式发送与服务地址一致的 `Origin`，并在同一个 HTTP 会话保留登录 cookie；不要把密码、JWT 放进命令行参数、示例脚本或日志。浏览器会自动携带同源 Origin。

## Architecture at a glance（当前主链路）

主链路的 Agent 循环在 `pi-harness` 中；本仓库提供它调用的**全部业务能力**，
以及所有权威状态的落点。

```text
Customer Browser / authenticated API client
        ↓
pi-harness: Pi Session → Main Agent → Tool Selection
        ↓  （内部 HTTP，每轮一枚短期 Service JWT）
python-impl /internal/*  ← 服务间通道，不对公网暴露
   ├─ /internal/auth/verify        身份解析，account → business_user 唯一权威
   ├─ /internal/tools/execute      READ 工具；剥离模型传入的身份字段，
   │                               从可信 claims 重新绑定 user_id
   ├─ /internal/tools/execute      WRITE 工具（仅 live 模式可达）
   │                               授权 → operation 落库 → 才执行
   ├─ /internal/context/turn-snapshot   每轮权威业务事实快照
   ├─ /internal/compliance/review  规则掩码（始终）+ 可选 LLM 复核
   ├─ /internal/memory/enqueue     长期记忆 outbox 消费端
   └─ /internal/operation_status   写操作结果的权威裁决（ExecutionLedger）
        ↓
ToolExecutor
  ├─ confirmation / approval（仅 high-risk）
  ├─ timeout / READ retry
  └─ idempotency（client_request_id + canonical payload hash）
        ↓
Business Domain（order / refund / ticket）→ SQLite + ExecutionLedger
        ↓
Knowledge：HybridRetriever = Dense(FAISS) + Sparse(BM25) → RRF → Cross-Encoder
```

工具传输的最终边界是**混合**的，这是刻意选择而非过渡态：

- `knowledge_search` 走 **MCP**（streamable HTTP）：共享语料、无 per-user 身份，
  渠道只需要一个服务级 token；
- `order_query / ticket_query / refund_evaluate / risk_check / refund_confirm /
  ticket_create` 走**内部 HTTP**：它们依赖 per-turn 身份信封
  （account / business_user / session / client_request），MCP 的长连接模型
  无法自然表达这一点。

## Architecture summary

- **权威状态在 Python，不在 Agent**。Agent output is not authoritative business state；
  transcript 也不是业务事实。
- `ChatOrchestrator` / `IntentRouter` / `KnowledgeRAGAgent` / `TicketHandler` /
  `RefundHandler` 是同一仓库中的 **legacy 路径**：仍由 `harness_version='legacy'`
  的会话与通用 `/api/chat` 使用，能力与业务域完全共享（同一个 `ToolExecutor`、
  同一套领域服务）。它不是新功能的主链路，Pi 会话走的是上面的 `/internal/*` 通道。
- 聊天链路中的副作用写入统一经过 `ToolExecutor`；`WRITE` 工具在非 `live` 部署下
  在服务端就不可达，而不是"被提示词劝阻"。
- 退款和工单效果由业务域写入 SQLite，`ExecutionLedger` 记录执行幂等和 replay。
- 工单使用 `client_request_id` 加 canonical payload hash 表示业务请求身份，跨用户或
  payload 冲突不会返回旧工单信息。
- 崩溃恢复见 [docs/recovery.md](docs/recovery.md)：收据（`agent_run_receipt`）与
  长期记忆 outbox 都从**持久状态**恢复，不依赖任何进程内状态。
- Eval 检查路由、无副作用、安全边界和故障收敛等不变量，不使用 LLM-as-judge。

简历表达、面试问答和演示脚本见 [docs/resume_interview.md](docs/resume_interview.md)。

## 技术栈

| 组件 | 技术 |
|------|------|
| Agent 运行时（主链路） | `pi-harness`，Node 22 / TypeScript / Pi SDK |
| Agent 编排（legacy 路径） | 显式 async `ChatOrchestrator` |
| HTTP 框架 | FastAPI + Uvicorn |
| LLM 调用 | LangChain `ChatOpenAI` |
| 向量检索 | FAISS |
| 短期记忆 | Redis async client + in-process fallback，Redis 状态带 TTL |
| 聊天断点 | MySQL + PyMySQL，节点快照、乐观版本检查、会话连接锁 |
| 追踪 | OpenTelemetry，可选 OTLP-compatible collector |
| 协议 | MCP 工具语义，HTTP `/api/tools`，内部 JSON-RPC 处理函数 |

## 快速开始

```powershell
# 安装依赖
pip install -r requirements.txt

# 配置环境变量
# 本机运行复制 .env.example；Docker 运行复制 .env.docker.example
Copy-Item .env.example .env
Copy-Item .env.docker.example .env.docker
# 编辑 .env / .env.docker，填入自己的 OPENAI_API_KEY
# OPENAI_API_KEY=...
# OPENAI_BASE_URL=...
# MODEL_NAME=deepseek-v4-flash
# EMBEDDING_BACKEND=local
# EMBEDDING_MODEL=BAAI/bge-m3
# RAG_INDEX_ROOT=./artifacts/rag_round3/production_indexes
# RAG_RERANKER_BACKEND=cross_encoder

# 抓取网页并生成 RAG Markdown
python -m scripts.fetch_knowledge_sources --config .\knowledge_sources\urls.yml --timeout 45 --min-clean-chars 200

# 构建 FAISS 向量库
python -m scripts.ingest_knowledge_base --kb-dir .\knowledge_base\generated --index-path .\vector_store\faiss_index --reset

# 运行测试（Mock LLM，无需 OPENAI_API_KEY）
pip install -r requirements-dev.txt
python -m pytest -q

# 启动服务
python -m api.main
```

服务启动后访问 http://localhost:8000/docs 查看 Swagger UI。

## 项目结构

```
python-impl/
├── agents/                     # Agent 实现
│   ├── orchestrator.py         # 显式请求编排
│   ├── intent_router.py        # 意图路由 Agent
│   ├── knowledge_rag.py        # RAG 知识检索 Agent
│   ├── ticket_handler.py       # 工单处理 Agent
│   └── compliance_checker.py   # 合规审查 Agent
├── memory/                     # 会话与长期记忆
│   ├── session_store.py        # 会话状态与消息边界
│   ├── short_term.py           # Redis / fallback 消息与状态后端，30 min TTL
│   └── long_term.py            # 长期记忆，FAISS 向量库
├── mcp/                        # MCP 工具协议
│   └── mcp_server.py           # 工具注册与调用，REST 暴露见 api/main.py
├── tickets/                    # 持久化客服工单业务域
│   └── service.py              # support_tickets 与客户端请求幂等
├── tracing/                    # OpenTelemetry 追踪
│   └── otel_config.py          # 追踪配置与 Agent 装饰器
├── api/                        # FastAPI 接口层
│   └── main.py                 # REST API 入口
├── knowledge_sources/           # 网页采集源、raw HTML、metadata、抽样审查
│   ├── urls.yml                 # 官方网页 URL 清单
│   ├── raw_html/                # 原始 HTML
│   ├── metadata/                # 每个 URL 的抓取和清洗元数据
│   ├── manifest.json            # 最近一次采集结果清单
│   └── review_samples.md        # 人工抽样校验预览
├── knowledge_base/              # RAG 入库文本
│   └── generated/               # 网页清洗后生成的 Markdown
├── scripts/                    # 运维脚本
│   ├── fetch_knowledge_sources.py # 抓取网页并清洗为 Markdown
│   └── ingest_knowledge_base.py   # 构建/更新 RAG 向量索引
├── tui/                        # 轻量终端聊天入口
├── requirements.txt
├── Dockerfile
├── compose.yaml                # API、Redis 与自动更新服务编排
├── .github/workflows/build-image.yml # PR 质量门禁，main push / dispatch 构建发布
├── .env.example                 # 本机运行环境变量示例
└── .env.docker.example          # Docker 运行环境变量示例
```

## 核心特性

### 显式请求编排

`ChatOrchestrator` 先恢复会话上下文，再调用 `IntentRouter`。置信度足够时，它最多选择一个响应分支，随后统一进行合规检查和响应合成；低置信度或未知路由直接返回澄清。问候、自我介绍、能力说明、致谢等进入 `ConversationAgent`，基于近期对话直接回复，不检索知识库或调用业务工具。具体产品知识和政策问题仍走 RAG；混合了寒暄的业务请求优先走业务分支。订单、退款和工单 Handler 通过 `ToolExecutor` 进入 MCP 和业务域，`KnowledgeRAGAgent` 通过共享 `HybridRetriever` 访问双域知识库。完整入口和分支见 [docs/architecture.md](docs/architecture.md)。

工单写入同时使用执行层 `idempotency_key` 和业务域 `client_request_id`，分别覆盖执行回放与客户端请求幂等。`refund_create` 和 `ticket_create` 当前都是 medium risk 写操作，需要确认、幂等键和 ledger，但不因 medium risk 自动要求人工审批。

### 会话状态

`SessionStore` 维护跨轮结构化状态，并通过 `sub_results["_session_context"]` 注入当前请求：

| 字段 | 写入时机 | 消费者 |
|------|---------|--------|
| `last_intent` | `IntentRouter` 完成后 | `intent_router.classify` 跨轮意图消歧 |
| `accumulated_entities` | 每轮实体合并（新覆盖旧） | `knowledge_rag` / `ticket_handler` 实体补全 |
| `turn_count` | 每轮递增 | 会话状态与调试 |

API checkpoint 路径以 MySQL 事件日志与快照为权威来源：`conversation_event` 表 append-only 记录完整会话流水，`session_digest` 保存滚动摘要与受保护字段，`agent_checkpoint` 只保留工作流恢复游标（不再复制整段消息历史）。`SessionStore` 在请求内使用独立上下文；Redis/进程内 Working Set 只是可丢失加速层，miss 或过期后从 MySQL digest + 最近事件 + checkpoint 无损重建。Redis 不可用、过期或含有旧 `pending_action` 都不会覆盖 MySQL；第一次使用新 checkpoint 时不会自动迁移旧 Redis 会话。上下文组装统一由 `context.ContextManager` 完成（接口见 [docs/context_manager_api.md](docs/context_manager_api.md)），预算与压缩阈值可用 `SMARTCS_CONTEXT_*` 环境变量调节（见 `.env.example`）。

不传 `checkpoint_store` 的离线编排器仍保留原 Redis / 进程内存兼容路径，包括旧 `[wm_snapshot]` 的一次性读取。该兼容路径没有工作流断点恢复能力。详见下节。

### MySQL 节点级断点恢复

API 启动要求可连接 MySQL，缺少密码、连接失败或保存失败会停止推进，不静默降级。配置 `MYSQL_HOST`（默认 `127.0.0.1`）、`MYSQL_PORT`（默认 `3307`）、`MYSQL_DATABASE`（默认 `smartcs_checkpoint`）、`MYSQL_USER`（默认 `smartcs`）和必填 `MYSQL_PASSWORD`。本地独立依赖见 `compose.checkpoint.yaml`；容器访问宿主 MySQL 时必须配置可达地址，不能使用容器自身的 `127.0.0.1`。

`agent_checkpoint` 包含 `id`、唯一 `session_id`、`user_id`、`state_json`、`status`、`version`、`created_at`、`updated_at`；`agent_checkpoint_request` 保存已完成请求回执，使旧请求在后续轮次之后仍可去重。快照只保存白名单文本消息和 JSON，不保存模型隐藏思考、API 凭据或可执行对象。

```text
PREPARED → ROUTED → EXECUTING → GENERATED → REVIEWED → FINISHED / WAIT_CONFIRM
```

`FINISHED` 对应设计方案的 `COMPLETED` 节点；状态使用 `running` / `finished` / `waiting`，其中 `waiting` 是已返回本轮回答、等待用户确认。恢复 `WAIT_CONFIRM` 只回放提示，必须通过新的聊天轮次明确确认或取消，不能代用户确认。

- `POST /api/chat` 可带 `client_request_id`。同一会话、同一 ID 和相同内容回放；相同 ID 不同内容返回 409；不同 ID 正常开始下一轮。有未完成请求时拒绝新消息，不能吞掉新消息恢复旧请求。
- `GET /api/checkpoints/{session_id}` 查询进度；`POST /api/checkpoints/{session_id}/resume` 只传可选 `client_request_id`，显式继续。身份来自登录状态。
- GET / DELETE history 先检查认证账号的会话归属，再由 checkpoint 检查业务身份。清理会话同时清理回执；正在执行或尚未完成的请求不能删除。
- 每会话使用 MySQL 连接持有的 `GET_LOCK`，每次更新使用版本 CAS。锁连接丢失后不能继续保存或执行新的写操作；不会按超时抢走仍在运行的请求。
- 工单分析结果、WRITE 参数、幂等键与确认依据先落盘，再调用工具；退款、工单和合规转人工都复用同一防重入口。恢复时针对当前键检查 stale ledger，默认仍需 60 秒；结果不明时返回冲突并保留断点，不盲目重写。

服务重启不会批量自动执行业务。未完成的模型生成步骤需要重新生成，不是逐 token 或模型内部状态恢复。MySQL 与业务 SQLite 之间没有分布式事务，恢复依赖已有权威业务查询，只覆盖当前退款/工单；不承诺任意外部工具 exactly-once。

前端按认证账号保存当前标签页的会话和请求 ID，刷新后恢复历史，未完成请求提供“继续处理”。JWT 仅在 HttpOnly cookie 中，不存入 JavaScript 存储。切换账号时清空页面数据。请求未到达服务时，以原消息和原请求 ID 重发，不覆盖另一个运行中请求。MySQL 会话、事件日志和回执无自动过期；对话历史以 append-only 事件为完整档案，清空历史使用 cutoff 事件而不删除原始事件。旧匿名 checkpoint 不自动关联到新账号，避免认领他人历史。

### RAG 管线

当前 RAG 流程：Query 改写 → 双域 Dense + BM25 → RRF 融合 → Cross-Encoder 重排 → 上下文注入 → 生成回答；检索最多进行两轮 refinement，最终回答仍使用原始问题。

Round 1 的离线索引构建位于 `rag/` 和 `scripts/build_rag_indexes.py`，把 `apple_support` 与 `agent_engineering` 分成独立的原始/规范化文本、结构化 chunk、FAISS dense artifact 和带词频的 BM25 corpus/index。构建命令默认使用确定性的 hash backend，不下载模型；生成的 manifest 会明确标记 `artifact_kind=dry_run`、实际 backend/model 与目标 embedding 配置 `BAAI/bge-m3`、1024 维，需实际模型时再显式传 `--embedding-backend local`。Markdown/TXT/PDF 均可加载，PDF 只提取嵌入文本；URL-only TXT 会保留为 unresolved 元数据而不会生成伪内容 chunk。Round 2 的 RRF、Cross-Encoder 与在线混合检索已在同一 `rag/` 层实现，Round 3 的固定双域 Retrieval Benchmark 见下文。

```text
python -m scripts.build_rag_indexes --domain all --reset
```

输出默认位于 `vector_store/rag_indexes/{apple_support,agent_engineering}/`，包括 `chunks.jsonl`、`corpus.jsonl`、`bm25_index.json`、`index.faiss` 和 `manifest.json`。现有 `memory/long_term.py` 与聊天 RAG 运行时保持兼容；本命令只负责离线索引，不替换当前在线检索链路。

### RAG Round 2 在线混合检索边界

Round 2 的在线链路位于 `rag/`：原始问题经现有 Query rewrite 后，统一进入双域 Dense + BM25 检索、RRF 融合和可注入 Cross-Encoder reranker，再把最终 Top-K 上下文交给回答模型。`KnowledgeRAGAgent` 与 MCP `knowledge_search` 共用 `HybridRetriever`；artifact manifest、chunk 顺序、FAISS 维度、BM25 chunk IDs 和实际 embedding backend/model 不一致时会 fail closed。Round1 的 `dry_run` artifact 只能通过显式 `allow_dry_run` 用于测试，生产路径默认拒绝。

Round 2 提供有界的最多两轮检索 refinement，并保留 `original_query` 用于最终回答。Round 3 已提供 60 条双域人工维护 query、可审计 graded qrels、manifest source hash 校验，以及 Dense/BM25/RRF/真实 Cross-Encoder 四路消融；headline 指标使用双域 global Top-20，不使用 query rewrite 或 oracle domain 过滤。`artifacts/rag_round3/metrics.json`、`metrics_by_domain.json`、`metrics_per_query.json` 和 `failure_analysis.md` 保存既有跑次结果；当前 v4 qrels 经审订，旧跑次指标不可直接当作 v4 基线。

BM25 中文切词优先使用 `jieba`；它未安装时会回退到内置词表和逐字切分。**安装 jieba 不会自动更新旧索引**，旧索引若按 fallback 构建，仅安装依赖会造成查询/索引分词不一致。要隔离重建、保留原 chunk ID 与 FAISS 向量，并在同一 benchmark 上比较 BM25、Dense 和 RRF（不含计算量较大的 Cross-Encoder 重排），可运行：

```bash
python -m pip install "jieba==0.42.1"
python -m scripts.rebuild_rag_sparse --input-root artifacts/rag_round3/production_indexes --output-root artifacts/rag_jieba_comparison/jieba_indexes
python -m scripts.evaluate_rag_tokenizer_ab --benchmark-root benchmarks/rag --baseline-root artifacts/rag_round3/production_indexes --candidate-root artifacts/rag_jieba_comparison/jieba_indexes --output-root artifacts/rag_jieba_comparison/round3_global --sparse-mode global_corpus_v1
```

如需双组真实重排评测，可给 `scripts.evaluate_rag_tokenizer_ab` 添加 `--include-rerank`，中断后使用相同参数加 `--resume` 续跑；结果目录首次运行必须不存在。`scripts.evaluate_rag_retrieval` 默认走本地域 BM25，不适合直接代替全局 BM25 双组对照。

本机现选择 **jieba 0.42.1** 用于 BM25 中文分词：已评测索引复制至 `artifacts/rag_jieba/production_indexes`，查询端与建索引端版本、词典一致，原 fallback 索引保留不覆盖。36 问 holdout 和 24 问跨域挑战集已完成真实重排对照；结果有升有降，本次切换是配置选择，不宣称已证明全面优于 fallback。已有原始 fallback 索引时，可在安装固定依赖后重建该目录（以下命令仅在输出目录尚不存在时执行）：

```bash
python -m pip install -r requirements.txt
python -m scripts.rebuild_rag_sparse --input-root artifacts/rag_round3/production_indexes --output-root artifacts/rag_jieba/production_indexes
```

全新 clone 不包含被忽略的原始索引，应从仓库知识源直接构建，不能运行上面的增量重建命令。以下命令使用本地 Hugging Face BGE-M3，不使用 hash / fake embedding；首次使用需下载模型，已有缓存可复用：

```bash
python -m pip install -r requirements.txt
python -m scripts.build_rag_indexes --domain all --embedding-backend local --embedding-model BAAI/bge-m3 --output-dir artifacts/rag_jieba/production_indexes
python -m scripts.build_global_sparse --artifact-root artifacts/rag_jieba/production_indexes
```

从知识源重新构建的 chunk 和向量未必与本机冻结索引相同，不能直接沿用旧 benchmark 的 chunk-ID qrels 或声称复现既有指标；严格 A/B 评测需使用原始冻结索引和对应 benchmark。

将 `.env` 中 `RAG_INDEX_ROOT` 设为该目录，保留 `RAG_SPARSE_MODE=global_corpus_v1`、`EMBEDDING_BACKEND=local` 和 `RAG_RERANKER_BACKEND=cross_encoder`，重启 API 后生效。`artifacts/` 被 Git 忽略，不会随代码推送。不要只把路径回改到 fallback 索引而仍用 jieba 查询；回退也必须保证索引/查询分词一致。Docker 侧已同步：`compose.yaml` 将 `./artifacts/rag_jieba` 只读挂载到容器 `/app/artifacts/rag_jieba`，`.env.docker.example` 的 `RAG_INDEX_ROOT=/app/artifacts/rag_jieba/production_indexes` 与 `EMBEDDING_MODEL=BAAI/bge-m3` 与冻结索引匹配；真实 `.env.docker` 需按示例自行同步。

评测脚本默认沿用 chunk-ID 计分；只有显式传入 `--qrel-groups PATH` 才按事实组计分。同组不同 chunk 命中只计一次，但仍占用原始排名位置。Holdout v1 已冻结为正式评测包；其 qrel 覆盖不声称全语料穷尽。

### RAG 向量库配置

长期记忆实现位于 `memory/long_term.py`，使用 FAISS 做本地向量索引，并把原文 chunk、来源文件、`doc_id`、`chunk_index` 等 metadata 一起保存，方便回答后追溯来源。

可选环境变量：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FAISS_INDEX_PATH` | `./vector_store/faiss_index` | FAISS 索引与 metadata 保存位置 |
| `EMBEDDING_BACKEND` | `hash` | `hash` 离线兜底；`local` / `sentence_transformers` 使用本地模型；`openai` / `remote` 使用远程 API |
| `EMBEDDING_MODEL` | `BAAI/bge-m3`（Round 3） | 本地 embedding 模型名；生产双域索引必须与构建索引的模型一致 |
| `EMBEDDING_DIM` | `1536` | 远程 embedding 维度提示，本地模型会自动读取维度 |
| `RAG_INDEX_ROOT` | 未设置时回退旧版 `FAISS_INDEX_PATH` | jieba 运行索引根目录：`./artifacts/rag_jieba/production_indexes`（需先构建） |
| `RAG_SPARSE_MODE` | `global_corpus_v1`（仅生产 `RAG_INDEX_ROOT` 路径） | 可设为 `domain_local_v1` 回滚；隔离/旧版检索仍用本地域 BM25 |
| `RAG_RERANKER_BACKEND` | `fake` | 生产索引必须使用 `cross_encoder`，测试 dry-run 才使用 `fake` |

本地双域生产检索使用以下配置。未设置 `RAG_INDEX_ROOT` 时，兼容路径会回退到旧版 `FAISS_INDEX_PATH`，不会自动加载 `agent_engineering` 生产索引。

```env
EMBEDDING_BACKEND=local
EMBEDDING_MODEL=BAAI/bge-m3
RAG_INDEX_ROOT=./artifacts/rag_jieba/production_indexes
RAG_SPARSE_MODE=global_corpus_v1
RAG_RERANKER_BACKEND=cross_encoder
```

切换 embedding 模型后必须重新入库，因为旧 FAISS 向量的维度和语义空间不能复用。面向生产检索时，建议显式设置 `min_score`，避免弱相关 chunk 被送进大模型。

### RAG 知识库入库流程

服务启动时不再写入演示知识。工程化流程是先构建向量索引，再启动 API 服务：

如果已经有整理好的 `.md` / `.txt` 文档，可以直接入库；如果来源是 HTML 网页，应先执行下一节的网页采集流程。

```powershell
python -m scripts.ingest_knowledge_base --kb-dir ./knowledge_base --index-path ./vector_store/faiss_index --reset
```

### 网页知识采集流程

如果原始资料来自 HTML 网页，先走可复现采集清洗，再入库向量化。URL 清单位于 `knowledge_sources/urls.yml`，采集脚本会保存：

| 路径 | 说明 |
|------|------|
| `knowledge_sources/raw_html/` | 原始 HTML，便于回溯和重新清洗 |
| `knowledge_base/generated/` | 清洗后的 Markdown，作为 RAG 入库输入 |
| `knowledge_sources/metadata/` | 每个 URL 的来源、抓取时间、清洗统计 |
| `knowledge_sources/review_samples.md` | 人工抽样校验用预览 |
| `knowledge_sources/manifest.json` | 本次采集结果清单 |

执行采集：

```powershell
python -m scripts.fetch_knowledge_sources --config ./knowledge_sources/urls.yml --timeout 45 --min-clean-chars 200
```

网页正文抽取链路为：

```text
httpx 抓取 HTML
→ trafilatura 抽取正文 Markdown
→ readability-lxml + html2text 兜底
→ 项目内置 HTMLParser 最后兜底
→ 站点噪声过滤与去重
→ metadata 记录实际 cleaner
```

`--min-clean-chars` 用于过滤登录页、JS 入口页、正文过短页面。被过滤的页面仍会保留 raw HTML 和 metadata，但不会写入 `knowledge_base/generated/`，避免污染向量库。

采集完成后，建议先查看 `knowledge_sources/review_samples.md`，抽样确认没有混入导航、页脚、Cookie 提示等噪声，再入库：

```powershell
python -m scripts.ingest_knowledge_base --kb-dir ./knowledge_base/generated --index-path ./vector_store/faiss_index --reset
```

如果要同时入库人工整理文档和网页清洗文档，可继续使用 `--kb-dir ./knowledge_base`。

此前 Apple 网页知识库的 URL 数量、过滤数量和 chunk 数量属于历史采集快照，不作为当前状态或验证结果。需要更新知识库时，请重新执行采集、抽样检查和入库流程。

入库脚本会读取 `--kb-dir` 下的 `.md` 和 `.txt` 文件，切分 chunk，写入 FAISS 索引和同名 `.meta.json`。metadata 至少包含：

| 字段 | 说明 |
|------|------|
| `doc_id` | 文档级稳定 ID |
| `chunk_id` | chunk 级稳定 ID |
| `content_hash` | chunk 内容哈希 |
| `document_hash` | 原始文档内容哈希 |
| `source_path` | 原始文件路径 |
| `chunk_index` / `chunk_count` | chunk 在文档中的位置 |
| `updated_at` | 本次入库时间 |

重复执行入库脚本是幂等的：文件内容不变时不会重复写入；文件内容变化时，会替换该文件对应的旧 chunk 并重建索引。

验证当前 embedding 和 FAISS 维度：

```powershell
python -c "from dotenv import load_dotenv; load_dotenv(dotenv_path='.env'); import faiss; from memory.long_term import create_embedding_backend; b=create_embedding_backend(); idx=faiss.read_index('./vector_store/faiss_index'); print(type(b).__name__, b.dimension, idx.d, idx.ntotal)"
```

期望输出类似：

```text
SentenceTransformerEmbeddingBackend 512 512 95
```

### 两阶段合规审查

1. 规则引擎做本地、确定性的敏感词匹配和 PII 检测。
2. LLM 合规检查处理规则未覆盖的越权承诺、隐晦违规等场景。
3. 高风险规则命中时直接拦截，不进入 LLM 检查。当前 LLM JSON 解码失败的 fallback 是通过，不能表述为安全 fail-closed。

### MCP 工具

默认工具集（业务调用见 `ticket_handler`、`refund_handler` 或合规转人工）：
- `order_query`：查询本地 SQLite 国内电商演示订单（`ticket_handler`）
- `refund_evaluate`：先检查订单归属和退款条件
- `refund_create`：经确认后创建退款申请，使用 `ExecutionLedger` 做执行幂等
- `ticket_create`：通过 `TicketService` 持久化创建工单，使用 `client_request_id` 和 payload hash 做业务幂等
- `ticket_query`：按工单号和用户归属查询持久化工单
- `knowledge_search`：复用主 RAG 的 `HybridRetriever`，支持 `apple_support` 与 `agent_engineering` 双域检索
- `risk_check`：已注册的风险查询工具

应用启动时，`knowledge_search` 复用主 RAG 的在线检索器，返回命中文档片段、来源、相似度分数和 metadata；传入 `domain` 或 `domains` 可限制知识域。
可通过 HTTP 直接验证：

```powershell
$body = @{
  name = "knowledge_search"
  arguments = @{ query = "Apple 账户密码恢复"; top_k = 3 }
} | ConvertTo-Json -Depth 3

Invoke-RestMethod -Method Post `
  -Uri "http://localhost:8000/api/tools/call" `
  -ContentType "application/json" `
  -Body $body
```

### RAG 降级

知识问答默认保留完整链路：查询改写 -> FAISS 检索 -> LLM 重排 -> 回答生成 -> 合规审查。
`RAG_ENABLE_QUERY_REWRITE=true` 与 `RAG_ENABLE_RERANK=true` 是默认配置，不应仅为了压缩延迟而关闭。

`REDIS_URL` 不可用时，短期记忆回退到进程内存，并在 `REDIS_RETRY_COOLDOWN_SECONDS` 冷却期内不重复尝试连接。该 fallback 不具备进程重启后的持久性；API checkpoint 路径改为读取 MySQL 恢复，不依赖 Redis。当前 RAG 的 LLM provider 异常会向上层传播，本文不宣称存在自动的 LLM failover。

### SQLite 电商业务 Sandbox

`order_query` 使用本地 SQLite 文件 `data/orders.db`。首次启动 API 或运行下面的初始化命令时，
系统会确定性地生成 100 笔国内电商风格的演示订单及商品明细，覆盖待付款、待发货、运输中、已签收、退款审核中、已退款和已取消等状态。
数据包含支付状态、实付金额、脱敏收货信息、快递公司、运单号、售后状态和创建时间，但不代表任何真实平台或用户数据。
业务数据分为 `users`、`orders`、`order_items`、`payments`、`shipments`、`refunds` 和 `support_tickets` 表。
`OrderRepository.get_order()` 保留原有字段，并附带 `payment`、`shipment` 和 `refunds` 详情；未发生支付或发货时对应值为 `None`，无退款时为 `[]`。
初始化会为旧版数据库补齐关联数据，重复运行不会重复造数。初始化脚本会输出各表的记录数。
本阶段只提供合成业务数据及本地 Sandbox 工具执行，不执行真实退款；审批接口仅用于本地运维演示。`refund_create` 和 `ticket_create` 是 medium risk 写操作，当前需要确认和幂等，不会因为 medium risk 自动进入人工审批。

运行确定性的业务状态模拟器（只在 CLI 层等待）：

```powershell
python -m scripts.run_business_simulator --db-path ./data/orders.db --ticks 10 --interval 1 --max-transitions 20 --create-orders 2
```

每次 tick 最多推进每笔订单一次；退款只完成已有 `refund_pending` 记录，不会从已签收订单自动发起退款。

手动初始化或修复本地数据库：

```powershell
python -m scripts.init_demo_orders
```

可直接验证：

```powershell
$body = @{
  name = "order_query"
  arguments = @{ order_id = "ORD-20260801-0001" }
} | ConvertTo-Json -Depth 3

Invoke-RestMethod -Method Post `
  -Uri "http://localhost:8000/api/tools/call" `
  -ContentType "application/json" `
  -Body $body
```

## API 接口

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/chat` | POST | 聊天 |
| `/api/auth/login`、`/api/auth/logout` | POST | 登录、退出；cookie 与来源检查 |
| `/api/auth/me` | GET | 当前认证账号 |
| `/api/sessions` | GET / POST | 自己的会话列表、服务端创建会话 |
| `/api/sessions/{session_id}` | GET / DELETE | 本人会话详情、删除 |
| `/api/history/{session_id}` | GET | 本人对话历史 |
| `/api/history/{session_id}` | DELETE | 清除本人非运行中会话快照与回执 |
| `/api/checkpoints/{session_id}` | GET | 本人节点状态；不暴露内部草稿或工具参数 |
| `/api/checkpoints/{session_id}/resume` | POST | 显式恢复；仅可选 client_request_id |
| `/api/demo/orders` | GET | 当前认证用户的订单 |
| `/api/tools` | GET | MCP 工具列表 |
| `/api/tools/call` | POST | 兼容调用入口；仅允许 READ 工具 |
| `/api/tools/execute` | POST | 客户允许的 READ 工具；WRITE 仅聊天入口 |
| `/api/approvals` | POST | 创建本地 Sandbox 审批记录 |
| `/api/approvals/{approval_id}` | GET | 查询本地 Sandbox 审批记录 |
| `/api/approvals/{approval_id}/approve` | POST | 批准本地 Sandbox 审批记录 |
| `/api/approvals/{approval_id}/reject` | POST | 拒绝本地 Sandbox 审批记录 |
| `/api/metrics` | GET | 系统指标 |
| `/api/metrics/runtime` | GET | 请求、工具和恢复的聚合运行时指标 |
| `/health` | GET | 健康检查 |

表中的审批接口及 `/api/metrics` 为兼容旧路径保留，但拒绝客户访问；内部审批服务仍供执行策略与离线测试使用。其他客户接口需要登录，健康检查与登录页面可匿名访问。CLI 调用需先登录并保留 cookie，不能直接照搬旧匿名请求示例。

## Runtime observability

- 外层 ASGI 请求包装器读取或生成 `X-Request-ID`，并把同一个 ID 返回到响应头。
- `/api/metrics/runtime` 返回 requests、tools、recovery 三类聚合指标。
- 本轮实现的运行时操作日志只记录经过清洗的请求、工具和恢复字段，不记录 arguments、result 或自由文本业务内容。该说明不覆盖项目中的所有日志。
- OpenTelemetry 可发送到可选的 OTLP-compatible collector；本地没有 collector 时可设置 `OTEL_SDK_DISABLED=true`。

## Verification

```powershell
python -m scripts.check_repository_readiness
python -m pytest -q
python -m evals.runner
python -m evals.runner --json
```

`pytest` 覆盖代码和集成回归，Eval runner 检查场景级 Agent 与业务不变量。当前默认离线回归最终为 `367 passed, 18 skipped`；认证专项和 checkpoint 专项均已包含在其中。2026-09-18 checkpoint 阶段的历史全量复验为 `284 passed in 58.14s`，验收报告见 [checkpoint_test_report.md](artifacts/checkpoint_20260918/checkpoint_test_report.md)，原始输出见 [full_suite.txt](artifacts/checkpoint_20260918/full_suite.txt)；同阶段原有 Eval 14 / 14 和 3 项 Node 测试的验收记录见同目录 `round1_acceptance.md`。复现真实 MySQL 测试需显式设置 `OTEL_SDK_DISABLED=true`、`EMBEDDING_BACKEND=hash` 和 `SMARTCS_CHECKPOINT_MYSQL_TEST=1`，并提供可用 MySQL。RAG runtime closure 的隔离语义、顺序回归、Node UI 和 API smoke 已于 2026-09-20 复验通过；这些结果不代表在线模型质量、真实流量、生产 SLA 或远程 CI 已执行。

真实 MySQL 定向验证必须显式开启，不会因本机存在 `.env` 自动运行外部集成测试：

```text
niu -c 'env OTEL_SDK_DISABLED=true EMBEDDING_BACKEND=hash SMARTCS_CHECKPOINT_MYSQL_TEST=1 python -m pytest -q'
niu -c 'env OTEL_SDK_DISABLED=true EMBEDDING_BACKEND=hash SMARTCS_CHECKPOINT_MYSQL_TEST=1 python -m pytest -q tests/test_checkpoint.py'
niu -c 'node --test tests/test_checkpoint_ui.cjs'
```

不开启该测试开关时，13 项 MySQL 用例明确 skip；默认离线 pytest 的生命周期测试使用 fake store。每次真实测试使用随机 session 和隔离临时 SQLite，结束只删除测试会话，不操作真实订单库。253 项早期验收与 270 项增量前测试均保留为历史基线，不作为当前数量；旧证据入口见 [简历与面试说明](docs/resume_interview.md#主张来源和边界)。

## Suggested Demo

1. 启动配置好 LLM provider 和认证密钥的 API，创建本地账号，在浏览器登录并新建会话。
2. 查询该用户名下的本地演示订单，展示路由和订单归属检查。
3. 使用同一用户、同一会话和一笔符合条件的订单发起退款请求，展示 `refund_evaluate` 的评估结果和会话中的待确认状态。
4. 回复确认，展示 `refund_create`、待确认状态清除和 SQLite 中恰好一个持久化退款效果。
5. 创建并查询一个持久化支持工单，展示业务请求幂等。
6. 查看 `/api/metrics/runtime`，再运行离线 Eval 输出。

不依赖 API 的确定性证据演示：

```powershell
python -m evals.runner
```

交互式 API 演示仍需要配置 LLM provider。

## Current Boundaries

- 业务数据使用本地 SQLite Sandbox，不代表真实平台或用户数据。
- 客户 API 已接入认证；无生产 RBAC，内部审批接口不向客户开放。
- checkpoint 有 MySQL 会话锁和 CAS，但本地业务 SQLite 不构成跨主机共享业务存储；不宣称完整多实例生产部署或分布式事务。
- 离线确定性 Eval 不测量在线 LLM 回答质量。
- 本次工作会话没有验证远程 GitHub Actions。
- 项目不声明生产流量、生产 SLA 或已部署状态。
- SSE 和更丰富的 UI 属于可选扩展，不是本地目标的必需条件。

## Terminal TUI

旧 TUI 仍保留在仓库，但其匿名 user_id 协议已被客户 API 拒绝，本轮不将 TUI 标为可用验收入口。请使用已登录的 Web UI 或保存登录 cookie 的 HTTP 客户端；以下 TUI 启动说明仅为旧客户端参考，不提供绕过认证的兼容开关。

先启动后端服务：

```powershell
python -m api.main
```

再打开另一个 PowerShell，在 `python-impl` 目录启动 TUI：

```powershell
python -m tui.app
```

也可以指定后端地址和用户 ID：

```powershell
python -m tui.app --base-url http://localhost:8000 --user-id user_001
```

TUI 内置命令：

| 命令 | 说明 |
|------|------|
| `/help` | 查看命令帮助 |
| `/health` | 检查后端健康状态 |
| `/history` | 查看当前会话历史 |
| `/session` | 查看当前会话 ID |
| `/exit` 或 `/quit` | 退出 TUI |

## Web 客服工作台

启动 API 后访问 [http://localhost:8000](http://localhost:8000)，可使用浏览器中的聊天工作台。
页面先要求登录，登录后支持历史会话、客服问题和自己的订单查询。
通过聊天流程查询时，使用完整订单号，例如：`查询订单 ORD-20260801-0001`。

### 测试

```bash
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -H "Origin: http://localhost:8000" \
  -b cookies.txt \
  -d '{"message": "我能咨询哪些问题？"}'
```

## Docker

Dockerfile 用于把 FastAPI 后端打包成可部署镜像。镜像内默认不打包真实 `.env` / `.env.docker`、`vector_store/`、raw HTML 和测试文件；运行时通过 `--env-file` 和 volume 挂载注入配置、向量库和模型缓存。镜像构建时会把 tiktoken `cl100k_base` BPE 文件烘焙进 `/home/app/.cache/tiktoken`，上下文预算计数在容器内无需任何运行期下载。

本机 Python 直接运行使用 `.env`；Docker 容器运行建议使用 `.env.docker`。不要提交真实 `.env` / `.env.docker`，仓库只提交 `.env.example` / `.env.docker.example`。

```powershell
Copy-Item .env.example .env
Copy-Item .env.docker.example .env.docker
```

两份配置最关键的区别是 FAISS 索引路径：

```env
# .env，本机 PowerShell 运行
FAISS_INDEX_PATH=./vector_store/faiss_index

# .env.docker，容器内部运行
FAISS_INDEX_PATH=/app/vector_store/faiss_index
```

原因是容器内部看不到 Windows 的 `D:\Workspace_for_Codex\...` 路径。启动容器时，下面这个 volume 会把宿主机的 `.\vector_store` 映射到容器里的 `/app/vector_store`：

```powershell
-v "${PWD}\vector_store:/app/vector_store"
```

所以容器内程序必须用 `/app/vector_store/faiss_index` 才能找到挂载进去的 FAISS 索引。

除 FAISS 外，Docker 运行还需要三块配置（`.env.docker.example` 已全部包含，真实 `.env.docker` 照抄后补密钥）：

- **MySQL checkpoint/用户记忆**：容器内 `MYSQL_HOST` 不能写 `127.0.0.1`（那是容器自身），Docker Desktop 用 `host.docker.internal`；先 `docker compose -f compose.checkpoint.yaml up -d` 启动独立 MySQL。
- **JWT 认证**：`AUTH_JWT_SECRET` 必须填入至少 32 字节的随机密钥，留空会拒绝启动；首次需在容器内运行 `python -m scripts.init_demo_auth_user` 建演示账号。
- **jieba 生产索引**：`compose.yaml` 已把 `./artifacts/rag_jieba` 只读挂载到 `/app/artifacts/rag_jieba`，容器内 `RAG_INDEX_ROOT=/app/artifacts/rag_jieba/production_indexes`；索引缺失或 embedding 模型不匹配时启动会 fail closed。

中央上下文预算可用 `SMARTCS_CONTEXT_*` 环境变量调节（默认值见 `.env.docker.example` 注释）。

先在宿主机完成网页采集与向量入库：

```powershell
python -m scripts.fetch_knowledge_sources --config .\knowledge_sources\urls.yml --timeout 45 --min-clean-chars 200
python -m scripts.ingest_knowledge_base --kb-dir .\knowledge_base\generated --index-path .\vector_store\faiss_index --reset
```

构建镜像：

```powershell
docker build -t smart-cs-python .
```

如果本机访问 PyPI 较慢，可切换到清华 PyPI 镜像：

```powershell
docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple -t smart-cs-python .
```

Dockerfile 会预装 CPU 版 PyTorch。项目使用 CPU 本地 Embedding，不需要 CUDA 和 NVIDIA 运行库；因此不要把 GPU 版 PyTorch 安装进镜像。

运行服务（临时前台模式，关闭终端会影响查看日志）：

```powershell
docker run --rm `
  -p 8000:8000 `
  --env-file .env.docker `
  -v "${PWD}\data:/app/data" `
  -v "${PWD}\vector_store:/app/vector_store" `
  -v smartcs-hf-cache:/home/app/.cache/huggingface `
  -v smartcs-st-cache:/home/app/.cache/sentence-transformers `
  smart-cs-python
```

推荐日常使用后台模式，并给容器固定命名：

```powershell
docker run -d --name smartcs-api `
  -p 8000:8000 `
  --env-file .env.docker `
  -v "${PWD}\data:/app/data" `
  -v "${PWD}\vector_store:/app/vector_store" `
  -v smartcs-hf-cache:/home/app/.cache/huggingface `
  -v smartcs-st-cache:/home/app/.cache/sentence-transformers `
  smart-cs-python
```

说明：

| 配置 | 作用 |
|------|------|
| `-d --name smartcs-api` | 后台运行容器，并使用固定名称便于 start/stop/logs |
| `--env-file .env.docker` | 注入模型服务、embedding、FAISS 路径等环境变量 |
| `-v "${PWD}\data:/app/data"` | 挂载本地 SQLite 演示订单，避免容器重建后重新生成 |
| `-v "${PWD}\vector_store:/app/vector_store"` | 挂载宿主机已构建好的 FAISS 索引 |
| `smartcs-hf-cache` | 缓存 HuggingFace 模型文件，避免每次容器启动都重新下载 |
| `smartcs-st-cache` | 缓存 sentence-transformers 模型文件 |

服务启动后，直接访问：

| 地址 | 作用 |
|------|------|
| http://localhost:8000/health | 健康检查 |
| http://localhost:8000/docs | Swagger UI，可视化测试 API |

常用容器管理命令：

```powershell
docker ps
docker logs -f smartcs-api
docker stop smartcs-api
docker start smartcs-api
```

如果启动时出现 `port is already allocated`，说明 8000 端口已经被旧容器或本机进程占用。先查看正在运行的容器：

```powershell
docker ps
```

如果旧容器就是本服务，可以直接继续使用 `http://localhost:8000/docs`；如果需要重启，先停止旧容器：

```powershell
docker stop smartcs-api
```

如果容器不是固定名称，使用 `docker ps` 输出里的 `NAMES` 停止，例如：

```powershell
docker stop lucid_johnson
```

日志中如果出现 `localhost:4317` / `OTLP` / `Failed to export traces`，通常只是 OpenTelemetry 追踪收集器未启动，不影响 `/health`、`/docs` 和 `/api/chat` 使用。需要追踪时再单独启动 Jaeger 或 OTLP collector。

### GitHub Actions 工作流配置

仓库包含 `.github/workflows/build-image.yml`。工作流配置为：

```text
Pull Request
    -> repository readiness
    -> pytest
    -> offline Eval
    -> 不构建和发布镜像

main push / workflow_dispatch
    -> 同一质量门禁
    -> image build / publish
```

镜像仓库名为：

```text
ghcr.io/acaia-77/smartcs:<tag>
```

Pull Request 只执行仓库就绪检查、测试和离线 Eval，不发布镜像。`main` push 和 `workflow_dispatch` 在同一质量门禁通过后，按 workflow 的标签规则构建并发布镜像，默认分支规则可能产生 `latest`，同时可以产生 commit SHA 等标签。workflow 使用 GitHub 自动提供的 `GITHUB_TOKEN`，不需要把 Docker Hub 或 GHCR 密钥写进仓库。本次工作会话没有执行或验证远程 GitHub Actions。

首次使用 GHCR 时，需要在 GitHub Packages 中将该镜像设置为 Public，或者在本机先执行 `docker login ghcr.io`。不要把个人访问令牌写入 `.env.docker` 或提交到 Git。

### 使用 Compose 更新 Docker 部署

`compose.yaml` 会统一管理 SmartCS API、Redis 和可选的 Watchtower。API 默认使用宿主机 `8001` 端口，避免与其他占用 `8000` 的服务冲突：

```powershell
# 首次使用：准备 Docker 配置
Copy-Item .env.docker.example .env.docker

# 当前手动部署已经创建过 smartcs-net 时无需执行；新机器首次部署时执行
docker network create smartcs-net 2>$null

# 如果旧 API 容器仍然占用相同名称，只删除旧 API 容器。
# 不要直接删除已有 smartcs-redis，先确认是否需要保留其中的会话数据。
docker rm -f smartcs-api 2>$null

# 拉取 GHCR 镜像并启动 API 与 Redis
docker compose pull smartcs-api smartcs-redis
docker compose up -d smartcs-redis smartcs-api
```

启动后访问：

```text
http://localhost:8001/
http://localhost:8001/docs
```

如果要继续使用宿主机 `8000`，在当前 PowerShell 会话中设置：

```powershell
$env:SMARTCS_HOST_PORT = "8000"
docker compose up -d smartcs-redis smartcs-api
```

如果要让本机 Docker 自动检查 GHCR 新镜像，每 5 分钟拉取一次并重建 API 容器：

```powershell
docker compose --profile auto-update up -d
```

Watchtower 只监控 `smartcs-api`，不会自动更新 Redis。它需要访问 Docker Socket；这是自动重建容器所必需的权限，因此只建议在个人开发机或受控测试环境启用。更新链路如下：

```text
main push / workflow_dispatch
    -> GitHub Actions 运行质量门禁
    -> 构建并推送 GHCR 镜像
    -> Watchtower 检测到 latest 变化
    -> 拉取新镜像并重建 smartcs-api
```

重新创建 API 容器不会删除宿主机挂载的 `vector_store/`、`data/`，也不会删除 Redis volume 中的会话数据。若 GHCR 镜像为 Private，需要先完成 Docker 登录：

```powershell
docker login ghcr.io
```

如果当前已经存在手动创建的 `smartcs-redis`，并且希望先只更新 API，可以保留旧 Redis，跳过 Compose 的 Redis 依赖：

```powershell
docker rm -f smartcs-api 2>$null
docker compose pull smartcs-api
docker compose up -d --no-deps smartcs-api
```

该方式要求旧 `smartcs-redis` 已经加入 `smartcs-net`，并且容器内地址仍为 `smartcs-redis:6379`。如果要让 Compose 接管 Redis，先确认不需要旧会话，或先使用 `docker exec smartcs-redis redis-cli BGSAVE` 和 `docker cp` 做备份，再删除旧 Redis 容器；不要使用 `docker compose down -v`，因为 `-v` 会删除 Compose 管理的持久化卷。
