# SmartCS × pi-agent 底座迁移计划（评估稿 v1）

> **用途**：本文档将发给 GPT 做第三方评估与完善，成熟后交回 Claude Code 执行。
> **读者假设**：读者不了解本项目的任何上下文，第 1 节为完整现状盘点。
> **日期**：2026-10-03 ｜ **作者**：Claude Code（基于 dg-piagent skill v0.83.0 基线 + npm/GitHub 官方 1.0.0 发布信息核查）

---

## 0. TL;DR

将现有 Python 智能客服系统（SmartCS）的 harness（编排/会话/工具调度层）迁移到 pi-agent SDK（`@earendil-works/pi-coding-agent`，TypeScript/Node）作为底座。**结论：技术可行，但本质是"换底座重写编排层"，不是渐进改造。** 推荐拓扑：**TS 新写 pi-harness 服务（会话 + 工具薄壳 + SSE + 扩展钩子），Python 侧保留 RAG 检索、记忆、幂等执行、数据层，经 HTTP/MCP 供 TS 调用**——项目最有差异化的资产（幂等账簿、两阶段写、用户记忆、上下文装配）因此无需重写。

---

## 1. 背景与现状盘点

### 1.1 系统是什么

SmartCS：面向 Apple 产品售后场景的多 Agent 智能客服系统。Python 3.12，FastAPI 对外，Docker Compose 部署（api + Redis 7 AOF + watchtower；独立 MySQL 8 实例承载 checkpoint/用户/记忆队列；订单与执行账簿在本地 SQLite）。

**关键事实：尽管常被描述为 "LangGraph 客服系统"，代码库中实际没有任何 LangGraph。** `scripts/check_repository_readiness.py:30` 明确禁止 `langgraph`/`StateGraph`/`MemorySaver` 进入生产代码（repo gate 直接 fail）。真实形态是**显式 async 编排器 + 手搓状态机**——这对迁移是利好消息：没有图运行时语义需要移植，阶段式管道可直接映射 pi 的「agent loop + 扩展 hooks」模型。

### 1.2 核心架构（现状）

```
POST /api/chat (JWT auth, 无 SSE——流式是规划项)
  └─ ChatOrchestrator (agents/orchestrator.py, 621 行)
       固定管道: prepare → route_intent → handle → compliance → synthesize
       checkpoint 阶段机: PREPARED→ROUTED→EXECUTING→GENERATED→REVIEWED→WAIT_CONFIRM|FINISHED
       ├─ intent_router     LLM JSON 分类 + 确定性正则纠偏（置信度 <0.7 → 澄清）
       ├─ conversation      闲聊，无工具
       ├─ knowledge_rag     查询改写 → HybridRetriever → 证据注入生成
       ├─ ticket_handler    工单查询/创建（写需显式用户同意短语）
       ├─ refund_handler    两阶段写：refund_evaluate(读) → pending_action → "确认退款" → refund_create(写, 幂等键 refund:{session}:{order})
       └─ compliance_checker PII/违禁词规则掩码 + LLM 合规复核
```

### 1.3 模块清单与规模

| 模块 | 文件数 | LOC | 职责 |
|---|---|---|---|
| `agents/` | 8 | 2,216 | 编排器 + 5 个专职 agent（纯类，`async process(state)`） |
| `api/` | 2 | 836 | FastAPI 全部端点；JWT HS256 HttpOnly cookie（30min TTL）；**无 SSE** |
| `auth/` | 5 | 207 | Argon2 + JWT + UserContext ContextVar（身份永不取自请求体） |
| `checkpoint/` | 3 | 1,062 | MySQL 事件溯源 + 阶段机 + 崩溃恢复 |
| `context/` | 8 | 2,753 | **中央上下文装配**：命名优先级 block + tiktoken 预算（soft 0.70/hard 0.85）+ WorkingSetCache(Redis) + `invoke_agent` 统一 LLM 入口 |
| `mcp/` | 17 | 2,892 | **进程内 JSON-RPC 工具服务器**（非标准 MCP 线协议）：7 个业务工具 + ToolExecutor（确认门/5s 超时/有界重试/幂等账簿 SQLite/审批/崩溃恢复） |
| `memory/` | 7 | 3,022 | 四层：Redis 短程(20 轮/1800s TTL/降级) · Redis session_state · FAISS 长期知识 · **用户记忆**(1,821 行：规则抽取→MySQL 租约队列→异步 worker，聊天路径只入队不等待) |
| `rag/` | 17 | 2,278 | 离线构建：FAISS(IndexFlatIP) + BM25(jieba) + RRF(k=60) + bge-reranker-v2-m3；artifact 制（production/dry_run 清单） |
| `tracing/` | 3 | 694 | OTel OTLP + `@trace_agent_call` + RuntimeMetrics |
| `tui/` | 3 | 221 | 最小 REPL，**已 stale**（早于 auth 改造，线上 API 会 400） |
| `scripts/` | 23 | 3,363 | RAG 抓取/构建/benchmark CLI |
| `tests/` | 61 | 12,788 | pytest 基线 367 passed / 18 skipped + `evals/` 离线确定性故障注入评测 |

**7 个业务工具**：`order_query`(读) · `refund_evaluate`(读) · `refund_create`(写,确认门,不可重试) · `ticket_create`(写,幂等) · `ticket_query`(读) · `knowledge_search`(读) · `risk_check`(读)。客户可调工具经 `_customer_arguments` 强制绑定 `user_id`（身份来自 auth context，不来自模型）。

**模型接入**：langchain `ChatOpenAI` → 任意 OpenAI 兼容端点（默认 DeepSeek，`OPENAI_BASE_URL` + `MODEL_NAME=deepseek-v4-flash`，temperature=0）。嵌入：本地 bge-m3 / OpenAI 兼容。重排：本地 cross-encoder。

---

## 2. 目标与非目标

**目标**
1. 编排底座换成 pi-agent（事件驱动 loop、session 管理、扩展机制、内建 compaction），消除手搓 plumbing 的维护成本。
2. 顺手补齐现状短板：SSE 流式（现状为零）、可用的 TUI/工作台、工具治理（exposure/namespace/outputSchema）。
3. 保留全部 Python 差异化资产：RAG、用户记忆、幂等执行、审批、数据层——零重写。

**非目标**
- 不重写 RAG/记忆/账簿/审批（保留为服务）。
- 不迁移历史会话数据（新底座新会话，旧数据只读归档）。
- 不改变现有 auth 语义（JWT + 用户绑定），TS 侧只做桥接。

---

## 3. pi-agent 版本选择

### 3.1 版本事实

- npm latest：**1.0.0**（2026-10-01 发布）。0.83.0 → 1.0.0 之间经历 0.84–0.87、0.99.x 共 14 个版本。
- **本计划直接钉 1.0.0**（`npm install @earendil-works/pi-coding-agent@1.0.0`）。全新项目无理由用旧版，且关键能力（内建 MCP、工具治理、classifier/virtual models）均为 0.99+ 才引入。

### 3.2 0.84 → 1.0.0 对本计划重要的变更

**新增能力（直接利好）**：
- **0.99.0 内建 MCP client**：stdio / streamable HTTP + OAuth，`mcp.json` 或 `pi.registerMcpServer()` 注册——Python 工具服务可包成真 MCP server 被直接挂载。
- **0.99.0 工具治理**：`exposure`（direct/model-only/codemode/deferred/hidden）、`namespace`、`annotations`、`outputSchema` + `structuredContent`。
- **0.99.0 virtual models / classifier models** + 0.86.0 `ctx.modelRegistry.complete()/stream()/streamSimple()`：扩展内可直接发起模型调用（意图分类的天然落点）。
- 0.86.0 prompt cache warming；`compaction.modelOverrides` 按模型配 reserveTokens/keepRecentTokens。
- 0.87.0 actionable turn 边界：`turn_end`/`agent_before_settle` 可返回 `{entries, continue: true}`（合规拦截/注入的官方位置）。
- 0.99.0 `session.steer()/followUp()` 成为正式 API；`pi.sendMessage(..., {triggerTurn:false})`。

**Breaking（影响设计，必须知晓）**：
- **Session v4 lane-based**（0.84）：`SessionManager` 成为 canonical；**直接赋值 `session.agent.state.messages` 不再生效**，恢复历史须 `SessionManager.inMemory(cwd, {id}, entries)` 或 `session.navigateTree()`。
- **`message_update` 只发 delta**（0.84）：累积 `message`/`partial` 字段移除，SSE 客户端须在 `message_start`→`message_end` 间自行拼接。
- **Custom provider 改 `TranscriptContext`**（0.86）：系统提示词和工具声明经 `getCurrentSystemPrompt()`/`getCurrentTools()` 读取。
- `shouldStopAfterTurn` 移除 → `finishTurn` 返回 `{action:"end"}`（0.87）。
- `ToolCall.arguments`/`ToolResultMessage.details` 限 JSON 兼容值（0.86）。
- `user_bash` 失败即终止（fail-closed，0.86）——安全利好。

### 3.3 开发基线声明

本计划撰写依据的 dg-piagent skill 文档核对到 **0.83.0**，与 1.0.0 存在上述漂移。执行期约定：**API 行为冲突时以 `node_modules/@earendil-works/pi-coding-agent/{CHANGELOG.md, dist/**/*.d.ts, examples/sdk/}` 为准**；正式开工后应将 skill 基线升级至 1.0.0。

---

## 4. 目标架构

### 4.1 拓扑

```
                        ┌──────────── 新写：pi-harness (Node/TS) ─────────────┐
  Web 工作台 / TUI ◀SSE─┤ Fastify/Express                                      │
  (stream + status)     │  ├ createAgentSession per 会话 (SessionManager.      │
                        │  │   inMemory + 显式 agentDir 隔离)                  │
                        │  ├ 系统提示词=客服人设 (覆盖 pi 默认 coding 人设)     │
                        │  ├ defineTool ×7: 薄壳 → 内部 HTTP 调 Python         │
                        │  ├ 扩展(pi.on): 意图路由 / 记忆注入 / 合规 / 审计    │
                        │  └ subscribe → SSE (text_delta / tool status / done) │
                        └───────────────┬──────────────────────────────────────┘
                                        │ 内部 HTTP（service token，携带 UserContext）
        ┌───────────────────────────────┼───────────────────────────┐
        ▼                               ▼                           ▼
┌─ Python: 业务执行服务 ──┐  ┌─ Python: RAG 检索服务 ──┐   ┌─ Python: 记忆服务 ──────┐
│ 现有 api.main 改造/共存  │  │ 从现 api 进程拆出        │   │ user_memory + worker    │
│ ToolExecutor/幂等账簿/   │  │ HybridRetriever 原样     │   │ 新增读取端点供注入       │
│ 审批/两阶段写 原样保留   │  │ FAISS+BM25+jieba+rerank │   │ MySQL 租约队列 原样      │
└───────────┬─────────────┘  └───────────┬─────────────┘   └───────────┬─────────────┘
            ▼                            ▼                             ▼
        SQLite 订单/账簿             vector_store artifacts          Redis + MySQL
```

### 4.2 职责划分原则

| 层 | 归属 | 理由 |
|---|---|---|
| Agent loop / 会话 / 流式 / 工具调度 / compaction | **TS (pi)** | pi 的核心价值，替代手搓 plumbing |
| 意图路由 / 系统提示词 / 合规判定位置 / 记忆注入时机 | **TS (扩展)** | 属于编排语义，必须在 loop 内 |
| 工具业务逻辑 / 幂等 / 审批 / 确认门执行 / 两阶段写落库 | **Python 保留** | 差异化资产，零重写；TS 工具仅是薄壳 |
| RAG 检索 / 嵌入 / 重排 | **Python 保留** | FAISS/jieba/bge 无合格 TS 等价物 |
| 用户记忆抽取/队列/worker | **Python 保留** | 1,821 行规则引擎 + MySQL 队列，新增读取端点即可 |
| Auth 签发与校验 | **Python 保留签发，TS 校验转发** | JWT HS256 共享密钥校验；UserContext 经内部头传递 |
| 合规规则执行 | 开放（见 Q6） | 规则可移植，但 LLM 复核留在 Python 更稳 |

### 4.3 工具接入的三种拓扑（请 GPT 重点比较）

- **方案 A：全量 TS 重写工具**——放弃 Python 执行层。❌ 否决：幂等账簿/审批/恢复全部重写，风险最高。
- **方案 B（推荐）：薄壳 HTTP 工具**——TS 侧 `defineTool` 7 个，execute 内调 Python 内部端点（现有 `/api/tools/execute` 已走 ToolExecutor 全套保障，需开内部 service 通道）。✅ 差异化逻辑零搬迁。
- **方案 C：真 MCP server**——Python 侧用官方 `mcp` 包把工具包成 stdio/HTTP MCP server，pi 1.0 内建 MCP client 挂载。✅ 协议标准化、工具发现免费；❌ 比 B 多一层协议适配，且确认门/幂等语义要穿过 MCP 表达。适合作为 B 稳定后的二期演进。

---

## 5. 组件映射总表

| 现状（Python） | pi 基底对应 | 迁移方式 |
|---|---|---|
| `ChatOrchestrator` 固定管道 + checkpoint 阶段机 | pi agent loop + 扩展 hooks（`before_agent_start`/`context`/`turn_end`/`agent_before_settle`/`finishTurn`） | 重写编排语义；loop 由 pi 提供 |
| `intent_router`（LLM JSON + 正则纠偏 + ORD/TK 实体抽取） | 首选 TS 移植（提示词+正则表是纯文本资产）；备选 pi classifier/virtual model 或扩展内 `ctx.modelRegistry.complete()` | 移植 |
| 5 个专职 agent | **单 agent + 按意图动态工具白名单**（推荐），或 subagent 工具（同进程子 session，H06 模式 3b） | 合并重写 |
| `mcp/` 进程内 JSON-RPC 工具服务器 | `defineTool` 薄壳（方案 B） | 薄壳重写 |
| ToolExecutor（确认门/幂等/审批/恢复） | **保留 Python**，TS 侧扩展只做「何时允许发起写工具」的门禁编排 | 不动 |
| FastAPI `/api/chat`（无流式） | Node SSE：`subscribe` 转发 `text_delta`→content、`tool_execution_start`→status、`agent_settled`+`prompt()` finally 双保险→done | 重写且补流式 |
| 四层记忆 | `SessionManager.inMemory()` + 自落库（多用户必须）；用户记忆在 `before_agent_start`/`context` 扩展事件注入 | 存储不动，注入层新写 |
| `context/` 中央装配（block + tiktoken 预算） | pi 内建 compaction 兜底；装配逻辑移植到 `context` 扩展事件（0.87 `context_with_system` 可取全量 transcript） | 概念对应，实现重写 |
| `tracing/` OTel | `pi.on("tool_call"/"tool_result")` + subscribe 事件 → OTel spans | 重写（注意：`pi.on` handler 被 await，落库必须 fire-and-forget 推队列） |
| TUI（stale） | pi 自带 TUI / web 工作台直打 SSE | 白得 |
| OpenAI 兼容端点（DeepSeek） | 内建 `openai-completions` provider（`baseUrl`+`apiKey`，Bearer 自动携带；必要时 `compat` 关 developer role/store） | 纯配置 |
| checkpoint 崩溃恢复语义 | pi v4 session 有 durable 记录但语义不同；业务级恢复依赖 Python 侧 `ExecutionReconciler` 原样保留 | 保留 Python |

---

## 6. pi 不提供、必须自建/保留的清单

1. **幂等执行账簿 + 崩溃恢复** → 保留 Python `ExecutionLedger`/`ExecutionReconciler`。
2. **审批流** → 保留 Python `approval_store`。
3. **两阶段写状态机**（refund pending_action「确认退款」流程）→ TS 编排层新写状态，Python 执行层保留。
4. **合规管道**（PII 掩码 + LLM 复核）→ 规则可移植，复核调用建议留 Python。
5. **审计级工具事件落库** → TS 扩展 `pi.on("tool_call"/"tool_result")` fire-and-forget 推队列，Python 消费落 MySQL。
6. **多租户会话存储** → pi 默认落盘 `~/.pi/agent/sessions/`（CLI 单用户设计），必须 inMemory + 自持久化。

---

## 7. 必改三项 + 多租户红线（pi 编码助手产品烙印）

1. **系统提示词必须覆盖**——默认硬编码 pi 人设（不覆盖会自称 "expert coding assistant operating inside pi"）。
2. **工具白名单必须收紧**——默认暴露 `read/bash/edit/write` 编码四件套；客服多用户场景 `bash` 是安全口子，必须全部禁用，只挂 7 个业务工具（`defaultTools` 设置 / `builtin:<name>` 禁用项）。
3. **会话存储必须接管**——`SessionManager.inMemory(cwd)`（**必须显式传 cwd**，默认 `process.cwd()` 会串）+ 自落库。

**多租户红线**：
- 不传 `agentDir` 时所有 session 共享 `~/.pi/agent`（auth.json/models.json）——多用户必须显式隔离或共享只读配置。
- 每个 session 必须 `dispose()`（同步；runtime 的 dispose 是 async 必须 await），否则监听器进程级泄漏。
- SSE 断连必须调 `unsubscribe()`（subscribe 返回值），否则监听器泄漏。
- 完成信号用 **`agent_settled`**（retry/compaction/steer 队列全消费完才派发），不要用 `agent_end`（retry 时 `willRetry:true` 提前触发）也不要用 `message_end`（一次提问触发多次）。
- **subscribe 静默收不到 6 个扩展独有事件**（`context`/`tool_call`/`tool_result`/`before_agent_start`/`input`/`model_select`）——监听它们不写分支报错、永不命中；需要这些事件必须写扩展走 `pi.on`。
- SSE 双层超时：单 turn 超时 + 总时长超时；客户端断开 `req.on("close")` → `session.abort()` + unsubscribe。

---

## 8. 分阶段实施计划

**Phase 0 — 技术验证 spike（1-2 天）**
- `npm i @earendil-works/pi-coding-agent@1.0.0`；最小 session：客服系统提示词 + `SessionManager.inMemory` + 2 个只读工具薄壳（`knowledge_search`、`order_query` 调 Python）+ SSE 端点。
- 验收：DeepSeek 端点跑通；流式 delta 拼接正确；`agent_settled` 完成信号可靠；扩展 `pi.on("tool_call")` 能拿到审计事件。

**Phase 1 — 只读链路对等（conversation + knowledge_rag）**
- 意图路由 TS 移植；JWT 桥接校验；RAG 检索从 api 进程拆为独立服务（或先内网直调）；记忆注入扩展（用户画像卡 + 近期 episode）。
- 验收：现有 `evals/` 中只读场景经 HTTP 黑盒重放，意图分布与回答质量不回归。

**Phase 2 — 写链路（ticket + refund）**
- `ticket_create`/`refund_evaluate`/`refund_create` 薄壳；两阶段写状态机（TS 编排 + Python 执行）；确认门编排；幂等键透传。
- 验收：重复提交/崩溃重放/并发同会话三类故障注入下，账簿无重复执行（复用现有 Python 侧测试策略）。

**Phase 3 — 合规 / 审批 / checkpoint 恢复 / 审计落库 / OTel**
- 验收：合规拦截在 `agent_before_settle`/`finishTurn` 生效；审计事件经队列落 MySQL 不阻塞 loop；OTel trace 贯穿 TS→Python。

**Phase 4 — 灰度切换**
- 按 intent 切流（先 conversation/knowledge_rag，后写操作）；web 工作台切 SSE；旧 FastAPI 聊天路径只读归档保留一个版本周期后下线。

---

## 9. 测试与验证策略

- **TS 侧离线测试**：pi Faux Provider（注册假 provider，不发真实 HTTP）+ 现有 pytest 业务断言经 HTTP 黑盒复用。
- **Python 侧**：367 passed / 18 skipped 基线必须全程不破（拆 RAG 服务时例外窗口需显式审批）。
- **回放评测**：`evals/` 故障注入场景对 pi-harness 黑盒重放，对比旧链路输出分布。
- **RAG 质量**：不受影响（检索服务原样），现有 benchmark 脚本继续作为门禁。

## 10. 风险登记册

| # | 风险 | 等级 | 缓解 |
|---|---|---|---|
| R1 | pi 1.0.0 发布仅 2 天，API 有未暴露的 breaking | 中 | 钉版本 + CHANGELOG/d.ts 兜底协议 + spike 先行 |
| R2 | 团队 TS 能力 / 双语言运维成本 | 高 | 这是战略成本，需决策层确认；TS 侧控制在 ~6-8k LOC |
| R3 | Node 单进程长会话内存增长（每 session 全量 messages） | 中 | dispose 纪律 + 会话空闲回收 + 水平拆分按 user 分片 |
| R4 | 两阶段写跨语言状态一致性（TS 编排态 vs Python 执行态） | 高 | 状态以 Python 账簿为准，TS 侧仅缓存视图；崩溃以 `ExecutionReconciler` 收口 |
| R5 | 合规 LLM 复核跨语言调用延迟叠加 | 中 | 异步复核 + 规则前置拦截；P95 预算单列 |
| R6 | 现有 13k 行 pytest 资产价值衰减 | 中 | 业务断言层保留为黑盒 HTTP 测试，不随实现报废 |

## 11. 开放问题（请 GPT 重点评估/挑战）

1. **单 agent + 动态工具白名单 vs 每意图 subagent**：现状 5 agent 合并为单 pi session 是否会损失隔离性？subagent（同进程子 session，8 并行/4 并发示例上限）值得吗？
2. **意图路由落点**：TS 移植正则表（简单但双语言维护提示词）vs pi classifier/virtual model（0.99 新能力，未经生产验证）vs 保留 Python 分类端点（多一跳）？
3. **方案 B vs C（HTTP 薄壳 vs 真 MCP）**：MCP 的协议成本换标准化是否值得二期再做？
4. **会话模型映射**：现有 MySQL 会话（账号维度）↔ pi inMemory session 的生命周期如何对齐？idle 回收、跨设备续聊（`inMemory(cwd,{id},entries)` 恢复）的条目从哪来？
5. **上下文装配**：现有 2,753 行 block assembler + tiktoken 预算，移植 TS 到 `context` 事件 vs 直接用 pi 内建 compaction + 少量注入？两者的 token 成本控制精度差异可接受吗？
6. **合规执行位置**：规则掩码移植 TS（低延迟）vs 调 Python 服务（单一实现源）？
7. **Node 部署形态**：pi-harness 与 Python api 同 compose 两服务 vs 三服务（再拆 RAG）？资源与故障域权衡？
8. **反向质疑**：有没有充分理由**不迁**（如把 pi 的事件/扩展模式借鉴回 Python 编排器）？请给出诚实的反方论证。

## 12. 工作量粗估

| 部分 | 估 |
|---|---|
| TS pi-harness 核心（会话/工具薄壳/路由/扩展/SSE/auth 桥） | 4-5k LOC |
| 记忆注入 + 合规编排 + 审计管线 | 1.5-2k LOC |
| Python 侧改动（内部 service 通道 + RAG 拆服务 + 记忆读取端点） | 0.5-1k LOC |
| 测试（TS + 黑盒复用改造） | 2-3k LOC |
| **总计** | **~8-11k LOC，3-5 人周（不含灰度观察期）** |

---

## 附录 A：pi-agent SDK 关键 API 速查（1.0.0）

```ts
import { createAgentSession, SessionManager, defineTool } from "@earendil-works/pi-coding-agent";
import { Type } from "@earendil-works/pi-ai";

// 会话：多用户必须 inMemory + 显式 cwd；生产自落库
const { session } = await createAgentSession({
  cwd: "/srv/smartcs",
  agentDir: "/srv/smartcs/.pi",            // 显式隔离，勿共享默认 ~/.pi/agent
  sessionManager: SessionManager.inMemory("/srv/smartcs"),
  // tools 白名单 / resourceLoader 覆盖系统提示词与技能
});

// SSE 转发（完成信号双保险）
const unsub = session.subscribe((e) => {
  if (e.type === "message_update" && e.assistantMessageEvent?.type === "text_delta") send("content", e.assistantMessageEvent.delta);
  if (e.type === "tool_execution_start") send("status", e.toolName);
  if (e.type === "agent_settled") sendDone();          // 推荐完成信号
});
try { await session.prompt(text); } finally { sendDone(); unsub(); }

// 业务工具薄壳
const orderQuery = defineTool({
  name: "order_query",
  description: "查询订单状态",
  parameters: Type.Object({ orderId: Type.String() }),
  async execute(id, params, signal, onUpdate, ctx) {
    const r = await pythonInternal.post("/api/tools/execute", { tool: "order_query", args: params }, { signal });
    return { content: [{ type: "text", text: r.text }], details: r.details };
  },
});

// 扩展（审计/路由/记忆注入）——pi.on handler 被 await，慢 I/O 必须 fire-and-forget
export default (pi) => {
  pi.on("tool_call", (e) => { auditQueue.push(e); });                 // 不落库，推队列
  pi.on("before_agent_start", async (e) => { /* 注入用户记忆/画像 */ });
  pi.registerTool(orderQuery);
  // pi.registerMcpServer(...)  // 方案 C 备用
  // ctx.modelRegistry.complete(...) // 扩展内发起分类调用
};

// 清理纪律
session.dispose(); // 同步；runtime.dispose() 是 async 必须 await
```

**OpenAI 兼容端点接入**（DeepSeek 现状）：内建 `openai-completions` provider，`baseUrl` + `apiKey`（支持 `$ENV` 引用），Bearer 头自动携带；非原生兼容 API 用 `compat: { supportsDeveloperRole:false, supportsStore:false, maxTokensField:"max_tokens" }`。

## 附录 B：现有基础设施契约（迁移期保持不变）

- Redis 7（AOF）：短程记忆 `smartcs:short_term:{session}`、session_state、WorkingSetCache。
- MySQL 8（:3307）：checkpoint 事件、平台用户/会话、用户记忆租约队列。
- SQLite（`data/orders.db`）：订单、退款、执行账簿、审批。
- RAG artifacts：`RAG_INDEX_ROOT=./artifacts/rag_jieba/production_indexes`（只读挂载），`RAG_SPARSE_MODE=global_corpus_v1`。
- LLM：`OPENAI_BASE_URL` + `OPENAI_API_KEY` + `MODEL_NAME=deepseek-v4-flash`，temperature=0。
- Auth：JWT HS256，`AUTH_JWT_SECRET` ≥32B，HttpOnly + SameSite=strict，身份永不取自请求体。
- OTel：`OTEL_EXPORTER_OTLP_ENDPOINT`（默认禁用，console 兜底）。
