# SmartCS × Pi Harness 迁移执行计划（v2 定稿）

> **状态**：设计定稿，执行中 ｜ **日期**：2026-10-03
> **修订记录**：2026-10-03 Phase 0/1/2/3 验收通过；2026-10-04 Phase 4/4B/5/6/6b 验收通过；**2026-10-04 Phase 7 验收通过**（Cohort 灰度：SHA-256 确定性分桶跨进程恒定、harness_version 创建即固定、统一入口 chat 分发 + pi 不可达宁 503 不降级——tripwire 硬门禁验证；含一次返修：chat 签名变序打破既有测试位置调用 + 替身旧接口，代码侧修复 + 批准替身两行对齐【对既有 legacy 测试文件的首次授权改动，范围限接口演进适配】）。终态数字：**pytest 636/37、TS 142/142、tsc 干净**（双方独占窗口数字逐项一致）。执行协调机制升级：跨会话消息协商 + 测试库独占窗口授予/归还/污染废弃口径。**计划主体阶段（0-7）全部完成。剩余：Phase 8（可选演进）与终局验收——待用户决策范围与交付方式（当前全部成果零 commit 留存于工作区）。**
> **本文档是三方合并的最终执行版**，取代以下三份过程稿（保留为决策记录，不再更新）：
> - `../pi-replatform-plan.md`（v1 评估稿，Claude）
> - `SmartCS_Pi_Harness_Claude_Execution_Review.md`（架构复核，GPT）
> - `pi-harness-claude-questions-resolution.md`（P1–P8 闭环，GPT）
>
> **迁移方向已冻结**，不再讨论"迁不迁"。目标：借重构学习成熟 Agent Harness，把 SmartCS 从"固定 async workflow + 一堆叫 Agent 的 Handler"升级为边界清晰的 Harness 架构，且**不牺牲**已有的用户隔离、显式确认、幂等、崩溃恢复、RAG 评测和业务状态一致性。

---

## 0. 核心设计原则（不可协商）

### 0.1 双层架构

```text
Pi (Node/TS) = Agent Runtime / Harness
Python       = Business Runtime
```

| Pi 负责 | Python 保留（最终权威） |
|---|---|
| Agent loop / LLM 调用 / 工具选择 | ToolExecutor / ExecutionLedger / ExecutionReconciler |
| transcript / compaction | Approval / 业务 pending state |
| system prompt / skills / extension hooks | 显式确认的业务约束（WriteAuthorizationService） |
| SSE 生命周期事件 / 工具调用生命周期 | RAG 检索与 rerank / 用户长期记忆 |
| Agent runtime observability | 用户/会话归属数据 / 业务数据库 |

**核心边界一句话**：Agent decides what it wants to do; the business decides whether it is allowed to happen.

### 0.2 四个不等式（最终验收标准）

```text
模型决策          ≠ 业务执行
Agent transcript  ≠ 业务状态
Agent session 恢复 ≠ side-effect 恢复
LLM 对确认的理解   ≠ 可信用户确认
```

### 0.3 Source of Truth 冻结表

| 状态 | Authority |
|---|---|
| session ownership / title / harness_version | MySQL `conversation_session` |
| Agent transcript / compaction / active branch | **Pi 原生 file-backed session 文件**（显式持久卷目录） |
| raw user message provenance | 最小 `memory_source_event` 账本（MySQL） |
| 请求是否已处理（request dedupe） | `agent_run_receipt`（MySQL） |
| 当前业务 pending state | Python / MySQL `pending_action`（从 Redis session_state 迁出，见 §5.4） |
| 可信 WRITE 授权 | Python `WriteAuthorizationService` |
| WRITE 副作用 | ToolExecutor + ExecutionLedger + Domain DB（SQLite） |
| 长期用户记忆 | Python Memory Store（MySQL 队列 + worker） |
| Node `AgentSession` 对象 | **运行时缓存，不是 authority** |

### 0.4 形态决定

```text
一个 SmartCS Main Agent + 一组确定性业务 Tools + 一组 Guards/Extensions + 独立 Business Runtime
```

不上多 Agent / Subagent（一期）。只有具备独立模型循环、上下文和自主工具决策的组件才配叫 Agent——现有 intent/refund/ticket/compliance "Agent" 全部降级为工具、规则与扩展。

---

## 1. 现状盘点（迁移输入）

**关键事实：代码库没有任何 LangGraph**（`scripts/check_repository_readiness.py:30` 明令禁止）。真实形态是显式 async `ChatOrchestrator`（`agents/orchestrator.py`，621 行）固定管道：prepare → route_intent → handle → compliance → synthesize。

| 模块 | LOC | 迁移处置 |
|---|---|---|
| `agents/`（编排器 + 5 专职 agent） | 2,216 | 编排语义由 pi loop 接管；意图正则纠偏、确认匹配等业务规则抽入 Python `WriteAuthorizationService` |
| `api/` FastAPI（JWT auth、sessions、chat、checkpoints、tools、history） | 836 | 保留为 Business Runtime 对前面孔 + 新增 internal_api；chat 主路径移交 TS |
| `checkpoint/`（MySQL 事件溯源 + 阶段机） | 1,062 | **拆职责**：workflow stage 删除；session lock / request receipt 迁移保留；event history 由 Pi transcript 替换；digest 由 compaction + snapshot 重估 |
| `context/`（中央装配 + tiktoken 预算） | 2,753 | **缩小职责而非删除**：transcript 压缩交 Pi；用户记忆选择/受保护字段/实体快照/pending 注入保留为 Python Context Service |
| `mcp/`（进程内 JSON-RPC 工具服务器 + ToolExecutor + 账簿 + 审批 + 恢复） | 2,892 | **原样保留**，是 Business Runtime 核心 |
| `memory/`（四层） | 3,022 | 保留；写入路径改 durable outbox（§6.6） |
| `rag/`（FAISS+BM25+jieba+RRF+bge-reranker，artifact 制） | 2,278 | **原样保留，不拆服务（一期）** |
| `tracing/`（OTel） | 694 | Python 侧保留；TS 侧重接（§6.9） |
| `tui/`（已 stale） | 221 | 不迁；pi 自带 TUI 仅作开发调试，不替代 web 工作台 |
| `tests/` + `evals/` | 12,788 | 基线 **512 passed / 37 skipped**（Phase 1 实测纠正，README 的 367/18 为陈旧冻结值）全程不破；旧 evals 冻结为 Business Runtime 回归套件（§9） |

**7 个业务工具**：`order_query`(读) `refund_evaluate`(读) `refund_create`(写,确认门) `ticket_create`(写,幂等) `ticket_query`(读) `knowledge_search`(读) `risk_check`(读)。

**LLM 接入**：OpenAI 兼容端点（`OPENAI_BASE_URL` + `MODEL_NAME=deepseek-v4-flash`，temperature=0）。TS 侧用 pi 内建 `openai-completions` provider 接入，配置即迁移。

---

## 2. 目标与非目标

**目标**
1. 编排底座换成 pi-agent（loop/session/compaction/事件/扩展），消除手搓 plumbing。
2. 补齐流式：SSE status 通道（合规约束下不做 token-by-token final，见 §6.5）。
3. 工具治理：7 个业务工具白名单化，编码工具全部禁用。
4. Python 差异化资产零重写。

**非目标（一期）**
- 不重写 RAG/记忆/账簿/审批；不拆 RAG 微服务；不做 MCP 化（Phase 8 再评估）。
- 不做 Subagent/多 Agent。
- 不迁移历史会话数据（旧 session 永远走 legacy harness）。
- 不做 token-by-token final answer 流式（与合规冲突；二期如需要须专门设计 incremental safety filter）。
- 不做多主机分布式 transcript（一期单实例 + 持久卷，见 §6.2）。

---

## 3. 版本冻结与审计程序（Task 2，开工第一步）

**当前事实（2026-10-03 核查）**：npm latest = **1.0.1**（当日发布，无 breaking；修复 provider "at capacity" 误杀 turn、brace-expansion 安全漏洞；**移除包内 npm-shrinkwrap** → 传递依赖不再被包方锁定）。

执行程序：
1. 开工当天 `npm view @earendil-works/pi-coding-agent version` 确认 latest。
2. 对照目标版本 `CHANGELOG.md` + `dist/**/*.d.ts` + `examples/sdk/` 输出 API delta（对照本文档引用的全部 API）。
3. **冻结确切 patch 版本**，`package-lock.json` 锁死（1.0.1 起包方不再锁定传递依赖，自有 lockfile 是必须项）。
4. 将 `dg-piagent` skill 基线从 0.83.0 升级到冻结版本后再正式编码（skill 维护流程）。
5. 迁移全程禁止自动升级 Pi。

**已知漂移提醒**（Phase 0 实测修正，详见 `pi-harness/PHASE0_REPORT.md` §6）：session 落盘版本为 **v3**（非 v4；v4 是 pi-agent-core harness 概念）；`message_update` 同时发 `text_delta` 增量与 `text_end`（`content` 已组装完整文本，自行拼接非必需）；`SessionManager` 是 canonical（对 `session.agent.state.messages` 赋值是瞬时突变，不改 transcript）；`shouldStopAfterTurn` 已移除改用 `finishTurn`；扩展经 `DefaultResourceLoader({ extensionFactories })` 注入（`createAgentSession` 无此参数），`noExtensions: true` 只关文件扫描不关 factories；显式 `tools` 白名单下内置编码工具**根本不注册**；SDK 内建 `buildSessionProjection()` 支持 append-only context edit（§6.8 history projector 直接复用）。

---

## 4. 目标架构

### 4.1 拓扑

```text
Web 工作台 / 客户端
   │  (公开: POST /api/chat [JSON] · POST /api/chat/stream [SSE] —— TS)
   │  (公开: sessions/history/tools 等其余端点 —— Python 保留)
   ▼
┌─ pi-harness (Node/TS) ──────────────────────────────┐
│ JWT 校验(shared secret) + 身份解析(经 Python 内部端点) │
│ SessionRegistry（per-session actor queue，单写者）     │
│ agent_run_receipt 检查点（dedupe/恢复）               │
│ SmartCS Main Agent (createAgentSession)              │
│  ├ 客服系统提示词（覆盖 pi coding 人设）               │
│  ├ 工具白名单：7 个业务工具薄壳（禁 read/bash/edit/write）│
│  ├ 扩展: 合规(message_end replacement)/审计/记忆注入   │
│  └ subscribe → SSE status / final / done             │
└───────────────┬─────────────────────────────────────┘
                │ 内部 HTTP + Internal Service JWT (aud=smartcs-business-runtime)
                │ TrustedTurnEnvelope{account_id, business_user_id, session_id,
                │   client_request_id, raw_user_message, raw_message_hash}
                ▼
┌─ Python Business Runtime (现有 api.main 扩展) ──────┐
│ internal_api/: auth · tools · context · memory ·      │
│   operation_status · history_delegate                 │
│ WriteAuthorizationService（新抽取）                   │
│ ToolExecutor → ExecutionLedger → Domain               │
│ ExecutionReconciler · Approval                        │
│ RAG（原进程内，不拆） · 用户记忆 worker                │
└──────────┬──────────────────────┬────────────────────┘
        Redis 7 (AOF)          MySQL 8 (:3307)         SQLite (data/orders.db)
        短程记忆/WS缓存         平台/会话/receipt/        订单/退款/账簿/审批
                               pending/记忆队列
Pi session 文件: 持久卷 SMARTCS_PI_SESSION_DIR=/var/lib/smartcs/pi-sessions/
```

### 4.2 目录边界

```text
D:\Workspace_for_Codex\project005_SmartCS\
├─ AGENTS.md            ← Phase 1 时更新（Node service 职责/命令/边界）
├─ python-impl\         ← 改动控制为适配层
│  └ internal_api\      ← 新增: auth.py tools.py context.py memory.py operation_status.py
└─ pi-harness\          ← 新增（与 python-impl 平级，属新架构决定）
   ├─ src\
   │  ├─ agent\     (create-smartcs-agent.ts · prompt\ · tools\ · extensions\)
   │  ├─ session\   (registry.ts · receipt.ts · lease.ts · idle.ts)
   │  ├─ business\  (python-client.ts · identity.ts · contracts.ts)
   │  ├─ streaming\ · tracing\ · server\
   └─ tests\        (unit · integration · e2e)
```

---

## 5. 状态模型

### 5.1 Platform Session（MySQL 权威）

`conversation_session` 新增字段 `harness_version = legacy | pi`。**session 创建时固定 harness，终身不变；禁止同 session 按 intent 跨 harness。**

### 5.2 Pi Transcript（Pi 文件权威）

- `SessionManager.create(SMARTCS_RUNTIME_CWD, SMARTCS_PI_SESSION_DIR, { id: smartcsSessionId })`——SmartCS session_id 直接作为 Pi session id，**不维护映射表**。
- **Durability point（Phase 0 实测收窄，D1）**：对**会话消息 entry**（user/assistant/toolResult 等）= 对应 SessionManager append 返回之后（同步写 JSONL）；对 **setup-only entry**（model_change/thinking_level/custom）**在首个会话消息前不落盘**（`_persist` 有 `_hasConversation()` 守卫）。因此**禁止依赖 `appendCustomEntry` 在首轮对话前持久化任何业务关键状态**——业务状态本就归 MySQL。idle 回收只做 `dispose()` + 从 registry 移除，**不承担保存职责**。
- 口径：**process/container crash-safe**（前提：持久卷）。不宣传主机断电级强持久（当前实现非每条 fsync）。
- 多主机共享存储/分布式 lease 留二期。
- **同 id 唯一性（Phase 0 实测，D4 裁决）**：`create(cwd, dir, {id})` 接受任意合法 id，但 **id 在目录内不唯一**（顺序两次 create 同 id → 两个文件共享 id，split-brain）；id→路径非纯函数（文件名含时间戳前缀），SDK 提供 `SessionManager.findById()` 做 O(n) 查找。**裁决：Phase 1 不建 `pi_session_registry` 表，改用"先查后建"纪律**——SessionRegistry 在 per-session mutex 内先 `findById`，命中则 `open`（须校验文件存在且 id 一致，`open()` 对缺失路径会静默新建空会话），未命中才 `create`；该纪律必须有测试（同 id 双 acquire 恰好产出一个文件）。多实例水平扩展时再评估建表。
- session id 字符集约束：`^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$`——SmartCS UUID 形态可用。

### 5.3 Request Receipt（MySQL 权威）

```text
agent_run_receipt
-----------------
session_id · client_request_id · request_hash
status = processing | completed | failed_recoverable
response (final answer JSON)
open_write_operations[]  (operation_id 列表, WRITE 发起前 durable)
created_at · updated_at
```

进入流程：`ownership → receipt lookup`：
- 同 id + 同 hash + completed → 直接 replay response
- 同 id + 不同 hash → 409
- processing → 进入恢复逻辑（§6.3），**禁止盲跑 prompt**

### 5.4 Business Pending State（Python/MySQL 权威）

`pending_action` 从 Redis session_state **迁至 MySQL**，带完整生命周期：

```text
pending_action: id · session_id · user_id · type · payload
status = pending | consumed | cancelled | expired
created_at · expires_at · operation_id
```

Pi 每轮经 snapshot 读取注入，但**绝不只存 transcript**（compaction 会吃掉它）。

### 5.5 Side-effect State

`ExecutionLedger` 是唯一权威。Pi transcript 里的 `tool_result` 只是 transcript，**不是业务执行成功凭证**。

---

## 6. 关键流程设计

### 6.1 请求生命周期

```text
POST /api/chat(/stream)
 → JWT 校验 + 身份解析（Python internal auth 端点，account→business_user 单一权威）
 → SessionRegistry: 取 session（无则按 harness_version=pi 建/按 id  reopen）
 → per-session mutex 排队（同会话严格串行；一期禁用 mid-run steer/followUp，
   并发 POST 在 mutex 排队，授权信封与 request 永远一一对应）
 → receipt 检查（§5.3）
 → durably persist USER_MESSAGE provenance（§6.6）
 → prefetch 并行: 业务状态快照 + 用户记忆快照（§6.7）
 → session.prompt()；扩展注入快照（轻量同步；pi.on handler 被 await，禁慢 I/O）
 → SSE: status 通道实时发；final 走 §6.5 缓冲-审核-发送
 → agent_settled → receipt=completed + memory_enqueue_pending=true → 响应
```

完成信号用 `agent_settled`（不用 `agent_end`——retry 时 willRetry 提前触发；不用 `message_end`——一次提问多次触发）。SSE 断连必须 `unsubscribe()` + `session.abort()`；双层超时（单 turn 60s + 总 5min）。**abort ≠ 业务失败**（§6.3 UNKNOWN）。

### 6.2 会话串行与 lease

一期：单 pi-harness 实例 + 持久卷 + per-session actor queue。跨设备同会话并发由 mutex 保证单写者。分布式 session lease（owner/timeout/崩溃回收/revision CAS）在水平扩展时引入，不提前实现。

### 6.3 WRITE Unknown Outcome 状态机

operation 状态：`PREPARED → SENT → COMPLETED | FAILED | UNKNOWN`。**权威是 Ledger/Domain DB，不是 Node 的 HTTP 返回值。**

铁律顺序（§10.1）：`LLM 请求写工具 → derive/allocate operation_id → durable 挂到 receipt → 才发 Python`。禁止先执行后记 id。

恢复决策表：

| Node 观察 | Ledger/Domain | 决策 |
|---|---|---|
| 尚未发送 WRITE | 无记录 | 可安全重新发起 |
| HTTP 成功 | COMPLETED | 恢复 tool result / final answer |
| 明确业务拒绝 | 已有确定结果 | 不重复副作用，返回业务结果 |
| HTTP timeout / socket 断 | 未知 | 标 UNKNOWN，**禁止 blind retry**，先 reconcile |
| crash 后 receipt=processing | COMPLETED | 从 ledger 恢复权威结果 |
| crash 后 receipt=processing | FAILED | 恢复失败结果 |
| crash 后 receipt=processing | 可证明未执行 | 才允许重新发起 |
| 无法确定 | UNKNOWN | 继续 reconcile，不得创建第二个 operation |

Pi transcript 缺 toolResult 的恢复：优先恢复成显式 recovery entry/continuation context；**若目标 Pi 版本无安全 transcript repair API，由 Harness 生成确定性"业务已完成"结果并结束该 request**；后续 turn 从 Python 业务状态重新注入事实。Phase 5 前必须 spike 选定恢复方式。

### 6.4 可信授权（WriteAuthorizationService）

**三种授权模式**（修正"所有 WRITE 两轮确认"的过度泛化）：

| 操作 | 模式 | 语义 |
|---|---|---|
| refund | `PREPARE_THEN_CONFIRM` | evaluate 生成 pending_action → 下一轮显式确认 → `refund_confirm(pending_action_id)` |
| ticket_create | `EXPLICIT_SAME_TURN` | 本轮 raw 文本已含明确创建意图（沿用现有 `_has_explicit_create_consent` 语义） |
| 合规升级工单 | `SYSTEM_POLICY` | 系统规则触发，system principal，不伪装用户确认 |

- `confirmed` **永远不是模型参数**。模型只提供业务意图与参数；`confirmed=True` 由 Python `WriteAuthorizationService` 计算：load pending_action → 验 owner/session → 确定性短语匹配 → 验未过期 → 决策 → ToolExecutor。TS 或模型错误调用 `refund_confirm` 时 Python **fail closed**。
- 信任链：Browser raw input → Node JWT → TrustedTurnEnvelope（含 raw_user_message + hash，hash 可与 provenance 账本互证）→ 签名内部请求 → Python 授权服务。
- **待对齐项（Phase 5 任务）**：盘点现有 `_has_explicit_create_consent` 的消息扫描窗口（仅当前轮 or 近 N 轮），ticket 授权窗口与之对齐并固化测试（如"agent 摘要→用户回'好的'"是否授权，以现状语义为准）。
- 工具暴露面：`knowledge_search · order_query · ticket_query · refund_evaluate · risk_check`（读）+ `refund_confirm · ticket_create`（写）。低级 `refund_create` 不暴露给模型，留在 Python 内部（`refund_confirm → ToolExecutor → refund_create`）。

### 6.5 合规与输出（Compliance-First Streaming）

**Eligible Final Assistant Message 定义**：assistant `message_end` 满足「正常 stopReason 且 content 无 toolCall」→ 记为 candidate；**latest wins**。带 toolCall 的 narration 不是 final。

```text
message_end(candidate) → 扩展内合规（规则掩码 → LLM 复核）→
  pass: 原消息 | sanitize: replacement message | fail: 确定性 fallback
→ Pi 持久化审核后的消息（用户所见 == transcript 所存）
→ 存 RunOutputBuffer
agent_settled → 取 latest candidate → SSE final → SSE done
无合法 candidate → 确定性安全 fallback
```

- **禁止** `text_delta` 作为 final 直发；实时通道只发**确定性 status**（由 tool name/runtime state 映射："正在查询订单"），**模型前置 narration 不得当 status 流出**。
- 合规复核在 `message_end` 扩展内（pi.on 被 await，会占 loop——final 已在关键路径上，与现状同步合规等价，属 parity 而非回归；P95 预算单列）。
- 保留非流式 `POST /api/chat` JSON 端点（final 本就缓冲后发送，成本为零）；新增 `POST /api/chat/stream`（SSE status/final/done）。Web 工作台一期不改前端。

### 6.6 用户记忆（Durable Outbox）

读取：`session.prompt()` 前 prefetch（记忆快照 + 业务快照并行），扩展轻量同步注入；**不在 pi.on 里 await 网络/MySQL**。

写入（崩溃安全，替代 fire-and-forget）：
```text
请求进入 → durably persist memory_source_event{event_id, session_id, user_id,
  client_request_id, content}（仅 USER_MESSAGE，非全量双写）
→ run 完成（含 WAIT_CONFIRM 的正常结束）→ receipt=completed + memory_enqueue_pending=true
→ 后台 dispatcher 扫描 → POST /internal/memory/enqueue
→ Python UserMemoryService.process_message()（保留 provenance 回查：owner/session/
  event/content 一致才抽取；assistant/tool 文本永不进入）→ 候选队列入 MySQL
→ mark memory_enqueue_done
```
记忆入队失败不触发 model/tool replay。

### 6.7 上下文职责切分

| 交 Pi | 留 Python Context Service |
|---|---|
| transcript / context window / generic compaction | 用户记忆选择 / 受保护业务字段 / 实体快照 |
| system prompt / skills / recent tool 对话 | pending state / 权威订单事实 / 领域 token 预算 / redaction |

每轮 `Business Context Snapshot`（user profile + pending action + 关键实体 + 近期已核实订单事实 + 记忆摘要）经扩展注入；**即使 Pi 压缩掉历史 transcript，关键业务状态仍可重新注入**。现有 2,753 行 Context Manager 缩小职责，不第一天删除。

### 6.8 历史与删除（harness-aware）

`GET /api/history/{session_id}`：前端 DTO 不变（user/assistant 的 role/content/created_at）；按 `harness_version` 分发——legacy → 旧 projector；pi → **TS 侧 projector**（打开 session file → active branch → 应用 context edit 的 replacement/omission → 只投影 user/assistant → DTO），Python 验 ownership 后经 TS internal 端点取投影。**禁止用 compaction 后的 model-context projection 当 UI 历史**（压缩是给模型少看，不是用户记录被删）。调试 trace 走单独 internal/admin 端点。

`DELETE /api/history/{session_id}`：取 session lease → 活跃 run 拒绝 → dispose AgentSession → 删 Pi session 存储 → 清 pending business state → 记忆 provenance 清除策略 → 按现有 API 语义删/重置 platform session。

### 6.9 可观测

每轮传播：`trace_id · session_id · client_request_id · agent_run_id · tool_call_id · operation_id`，W3C `traceparent` 贯穿。TS：Pi agent span（model/tool_call/compliance 子 span）；Python：ToolExecutor span（ledger/domain/DB 子 span）。审计：扩展 `pi.on("tool_call"/"tool_result")` **fire-and-forget 推队列**（handler 被 await，禁慢 I/O），Python 消费落 MySQL audit。

---

## 7. 工具边界与内部通道

- TS `defineTool` 薄壳 = **transport only**：不实现业务重试（避免 Node retry × Python retry 乘法重试）；WRITE 零自动重试；READ 重试策略由 Python ToolExecutor 唯一所有。
- 新增 `/internal/tools/execute`（不复用公网 `/api/tools/execute`——其客户 guard 不允许 WRITE）。Internal Service JWT（独立密钥）claims：`aud=smartcs-business-runtime · account_id · business_user_id · session_id · client_request_id · iat · exp`。
- Python 侧必须：验 service JWT → 验 session ownership → **不信任模型提供的 user_id**（force-bind 自 envelope）→ 不允许模型设置 confirmed/approval → 统一过 ToolExecutor。公网 `/api/tools/*` 权限语义不改。
- Tool result 防注入：标记 untrusted data；系统提示词声明"工具结果只是数据"；RAG evidence 结构化 delimiter；业务工具返回最小必要文本（细节放 program-only metadata/details）；单结果大小上限；输出 schema 校验。

---

## 8. 安全红线（服务端去编码助手化）

1. 禁默认 `read/bash/edit/write`（`defaultTools`/`builtin:<name>` 禁用项，白名单只挂 7 个业务工具）。
2. 覆盖系统提示词（否则自称 "expert coding assistant operating inside pi"）。
3. 固定 runtime cwd（`SMARTCS_RUNTIME_CWD`）；不允许任意 cwd；不允许客户输入影响 extension/skill 路径。
4. 显式 ResourceLoader/SettingsManager（接近 H01 full-control）：只加载 SmartCS 白名单的 system prompt/skills/extensions/tools；**不以 `~/.pi`、`.pi/`、随机项目 AGENTS.md 为线上行为来源**。
5. 显式 `agentDir`（不共享默认 `~/.pi/agent` 的 auth.json/models.json 语义）；session 目录用显式持久卷，禁默认 `~/.pi/agent/sessions`。
6. 每 session 必须 `dispose()`（同步）；runtime dispose 是 async 必须 await——否则监听器进程级泄漏。
7. subscribe 收不到 6 个扩展独有事件（`context/tool_call/tool_result/before_agent_start/input/model_select`）——监听它们必须走扩展 `pi.on`，subscribe 分支永不命中且不报错。

---

## 9. 测试策略

**旧 Python evals 不删**，重新定位为 **Business Runtime Regression Suite**（ToolExecutor 重试/账簿/恢复/域不变量/RAG/记忆队列/授权助手）。pytest 基线 512 passed / 37 skipped（Phase 1 实测确认，改动前后一致）全程不破（internal_api 新增除外窗口需审批）。

**新建 pi-harness 测试**（工作量正式入账，不做附属）：
- TS unit：registry/队列/receipt 状态机/output buffer/history projector/auth envelope/工具契约/status 映射/compliance candidate 选择。
- Pi runtime integration（Faux provider，不发真请求）：模型→工具、多轮工具、compaction、abort、agent_settled、session resume。
- 跨服务 E2E：HTTP/SSE → Node → internal → Python test runtime → 测试 DB。验 ownership/dedupe/副作用/恢复/history/记忆 outbox/final 合规。
- **业务 scenario 复用断言、不复用旧调用实现**（如 case_05 退款确认改为黑盒 HTTP 序列 + `refund_count` 断言）。

**故障注入矩阵 F1–F14（全部自动化或可重复）**：

| Case | 故障 | 必须保证 |
|---|---|---|
| F1 | Node 在 LLM 前崩溃 | 无副作用，request 可恢复 |
| F2 | READ 工具 HTTP 中断 | 可安全重试 |
| F3 | WRITE 发送前 Node 崩溃 | Ledger 无 operation，可重新发起 |
| F4 | Python WRITE 成功、HTTP 响应丢失 | UNKNOWN → reconcile，禁 blind retry |
| F5 | WRITE 成功但 Pi toolResult 未 append | 从 ledger 恢复，不得再执行 WRITE |
| F6 | final 生成后 receipt 完成前崩溃 | 不重复 WRITE，可重新生成/replay |
| F7 | 同 client_request_id 重发 | 不重复 turn/副作用 |
| F8 | 同会话两设备并发 | 单写者、确定顺序 |
| F9 | SSE 断开且 WRITE 执行中 | abort≠失败，走 reconcile；重连可查真实状态 |
| F10 | compaction 后确认退款 | pending 来自 Python 结构化状态，不依赖旧 transcript |
| F11 | tool result 含 prompt injection | 仅作不可信数据 |
| F12 | 进程/容器崩溃后同持久卷恢复 | 同 session/transcript/业务事实（多主机恢复属二期） |
| F13 | receipt=processing + lease/owner 失效 | 新 owner 先恢复，不从头盲跑；单 canonical response |
| F14 | pending_action 过期后迟到确认 | 拒绝旧确认，不产生 WRITE，提示重新评估 |

**质量门禁（迁移 Gate）**：turn 成功率 / 工具选择准确率 / 平均每轮工具调用 / 非必要调用率 / P50·P95 / token 用量 / compaction 频率；RAG 保留 Recall@10·MRR@10·nDCG@10·wrong-domain 并新增 retrieval-needed accuracy·tool invocation rate·groundedness·evidence usage·citation correctness；业务安全五零（越权/无确认 WRITE/重复副作用/跨用户泄漏/重放重复）；F1–F14 全过。

---

## 10. 分阶段计划

### Phase 0 — Pi Runtime Spike（不接真实 WRITE）

任务：冻结版本（§3）；Node 版本；DeepSeek provider（openai-completions + compat）；客服系统提示词；禁用全部编码内置工具；file-backed SessionManager；fake 读工具 ×2；事件生命周期；JSON final + SSE status；abort。

**SDK 断言清单（全部验证通过才准出，任一不成立则回炉对应设计）**：
- A1 `SessionManager.create(cwd, dir, {id})` 接受调用方指定 id（§5.2 无映射表设计的承重墙）；session id → 文件路径可推导性（否则落 `pi_session_registry`）。
- A2 `message_end` 扩展可返回 replacement message（§6.5 合规设计的承重墙）。
- A3 事件顺序：extension `message_end` → public listeners → `SessionManager.appendMessage`。
- A4 file-backed append 的落盘时机（durability point 定义是否成立）。
- A5 同 session id 被两个进程打开的行为（单实例假设的保护网）。
- A6 `agent_settled` 每 prompt 恰好一次、retry/compaction 后派发。
- A7 reopen 同一 session（`SessionManager.open`/等价 API）恢复 active branch。
- A8 `defaultTools`/白名单禁用内置工具在 server 模式下生效。

验收：agent loop 可跑；session 可外部恢复；tool_call hook 正常；无 bash/read/write/edit；SSE 生命周期正常。

### Phase 1 — Session / Receipt Foundation（业务 WRITE 门禁）

任务：`harness_version`；显式 session dir + 持久卷；SessionRegistry + per-session mutex；`agent_run_receipt`；`memory_source_event`；idle dispose/reload；request replay；history projector + DELETE 语义；JWT 桥接 + internal auth 端点；根 AGENTS.md 更新。

验收故障：进程重启 / 容器重启 / 重复 request_id / 同 id 不同 payload(409) / 同会话双请求串行 / idle 恢复。**不通过禁止进入 Phase 2。**

### Phase 2 — 只读业务工具

接 `knowledge_search/order_query/ticket_query/refund_evaluate/risk_check`：internal service JWT；ownership；模型不可控 user_id；Python 仍是 tool policy owner；RAG 不拆服务；意图分类器降级为观测标签（metrics/UI/eval bucket），主路由由 Main Agent 工具选择承担。

验收：无越权查询；read parity；RAG benchmark 不回归 + E2E retrieval/tool-choice eval。

### Phase 3 — Context / Memory / Compliance

Business Context Snapshot；记忆 prefetch + Durable Outbox；Pi compaction 接入；eligible final candidate + `message_end` 合规 replacement；JSON final / SSE status。

验收：compaction 后 pending 事实不丢（F10）；记忆不串用户；未审核 token 不外发；PII 用例无"先流出后替换"。

### Phase 4 — WRITE Shadow

Pi 只产生 write tool plan 不执行，与 legacy 对比：是否退款/建单、参数一致性、是否要求确认、授权模式正确性。达阈值才进 Phase 5。

### Phase 5 — WRITE Enable + Recovery

`refund_confirm · ticket_create · system escalation`；WriteAuthorizationService 抽取（含 ticket 授权窗口对齐任务，§6.4）；operation_id-before-send；UNKNOWN + reconcile；transcript repair 方式 spike 定案；F1–F14 全量。

### Phase 6 — Observability

TS span → trace context → Python span 全链路；六 ID 统一；审计队列落库。

### Phase 7 — Cohort 灰度

新 session 按 `hash(account_id)` 或新会话队列分桶（如 5%/95%）；`harness_version` 创建时固定；**禁按 intent 切**；旧 session 永远 legacy。

### Phase 8 — 可选演进

READ 工具 MCP 化评估；Skills 渐进披露（政策/话术/SOP，**不作业务 authority**）；Subagent 评估（非成功条件）；多主机 lease + 分布式 transcript 评估。

---

## 11. 风险登记册

| # | 风险 | 等级 | 缓解 |
|---|---|---|---|
| R1 | Pi 1.0.x 新发布，存在未暴露 API 问题 | 中 | §3 冻结程序 + A1–A8 spike 断言先行 |
| R2 | 团队 TS 能力 / 双语言运维 | 高 | 战略成本，决策层确认；TS 侧控制规模 |
| R3 | Node 单进程长会话内存增长 | 中 | dispose 纪律 + idle 回收；水平扩展走 §6.2 二期 |
| R4 | 两阶段写跨语言状态一致性 | 高 | Python 账簿为准，TS 仅视图；Reconciler 收口 |
| R5 | 合规 LLM 复核延迟叠加 | 中 | final 本就缓冲发送（parity）；P95 单列预算 |
| R6 | 旧 pytest/evals 资产价值衰减 | 中 | 冻结为 Business Runtime 回归；scenario 黑盒化复用 |
| R7 | A1/A2 等 SDK 断言不成立导致局部返工 | 中 | Phase 0 门禁设计就是为此；备选方案已预留（§6.3 repair 降级、§5.2 registry 表） |

## 12. 硬约束（执行时不得自行更改）

1. 不重写 RAG / ToolExecutor；不删 ExecutionLedger / Reconciler。
2. Pi transcript 不作业务退款状态权威；模型不提供 trusted identity / trusted confirmation。
3. Node 薄壳不实现 WRITE 重试；不按 intent 灰度；Phase 1 不顺手拆 RAG 服务。
4. 不开 read/bash/edit/write；不用用户 home `.pi` 资源作线上行为来源。
5. 不混用 agent-core 历史 Harness API 与 coding-agent 1.0.x SessionManager。
6. 不上 Subagent（一期）；不为 MCP 而 MCP；不在合规前流出 final token。
7. 不长期双写两套 conversation transcript（记忆 provenance 最小账本除外）。
8. idle 回收不承担保存职责；durability point = SessionManager append 返回。
9. 先 durable operation_id 再发 WRITE；UNKNOWN 禁 blind retry。
10. 记忆写入走 durable outbox；assistant/tool 文本永不进记忆抽取。

## 13. 工作量重估（取代 v1 的 3–5 人周承诺）

| 部分 | 估 |
|---|---|
| TS pi-harness（agent/session/receipt/streaming/extensions/auth 桥） | 5–7k LOC |
| TS 测试（unit + integration + E2E + 故障注入 + scenario 移植） | 3–4k LOC |
| Python internal_api + WriteAuthorizationService 抽取 + pending 迁移 | 1–1.5k LOC |
| **合计** | **9–12.5k LOC；周期以阶段门禁为准，不做日历承诺** |

## 14. 开工门禁：12 问的确定答案

| # | 问题 | 答案（索引） |
|---|---|---|
| 1 | Pi exact version | 1.0.1（开工日复核后冻结，§3） |
| 2 | transcript canonical storage | Pi file-backed JSONL，显式持久卷（§5.2） |
| 3 | crash 后 reopen 同 session | 按 id reopen（A1/A7 验证），ownership 先行（§6.1） |
| 4 | 同会话并发串行 | SessionRegistry actor queue；禁 mid-run steer/followUp（§6.1/6.2） |
| 5 | client_request_id exactly-once | agent_run_receipt 三态 + replay/409/恢复（§5.3） |
| 6 | operation_id durable 时机 | 发 WRITE 前挂 receipt（§6.3 铁律） |
| 7 | timeout 后 FAILED vs UNKNOWN | 一律 UNKNOWN→reconcile；仅确定业务拒绝例外（§6.3 表） |
| 8 | 谁有权 confirmed=True | 仅 Python WriteAuthorizationService（§6.4） |
| 9 | ticket vs refund 授权差异 | EXPLICIT_SAME_TURN vs PREPARE_THEN_CONFIRM；窗口对齐为 Phase 5 任务（§6.4） |
| 10 | memory provenance + durable enqueue | memory_source_event + outbox dispatcher（§6.6） |
| 11 | history 分发 | harness-aware projector，TS 投影 pi，Python 验权（§6.8） |
| 12 | final 定义/审核点/发送点 | eligible candidate + message_end replacement + settled 后发送（§6.5） |

---

## 附录 A：pi-agent SDK 速查（1.0.x，最终以冻结版 d.ts 为准）

```ts
import { createAgentSession, SessionManager, defineTool } from "@earendil-works/pi-coding-agent";
import { Type } from "@earendil-works/pi-ai";

// 会话（file-backed，显式目录与 cwd；{id} 待 A1 验证）
const { session } = await createAgentSession({
  cwd: process.env.SMARTCS_RUNTIME_CWD!,
  agentDir: process.env.SMARTCS_PI_AGENT_DIR!,       // 显式，勿用默认 ~/.pi/agent
  sessionManager: SessionManager.create(
    process.env.SMARTCS_RUNTIME_CWD!,
    process.env.SMARTCS_PI_SESSION_DIR!,
    { id: smartcsSessionId },
  ),
});

// SSE（完成信号双保险；message_update 只有 delta，自行拼接）
const unsub = session.subscribe((e) => {
  if (e.type === "tool_execution_start") send("status", statusFor(e.toolName));
  if (e.type === "agent_settled") sendFinalAndDone();   // final 来自 RunOutputBuffer
});
try { await session.prompt(text); } finally { sendDone(); unsub(); }

// 工具薄壳（transport only，不写重试）
const orderQuery = defineTool({
  name: "order_query",
  description: "查询订单状态（只读）",
  parameters: Type.Object({ orderId: Type.String() }),
  async execute(id, params, signal, _onUpdate, ctx) {
    const r = await pythonInternal.executeTool("order_query", params, envelope(ctx), { signal });
    return { content: [{ type: "text", text: r.text }], details: r.details };
  },
});

// 扩展：合规 replacement（待 A2 验证）/ 审计 fire-and-forget / 快照轻量注入
export default (pi) => {
  pi.on("message_end", async (e) => { /* 合规审核 → replacement（A2） */ });
  pi.on("tool_call", (e) => { auditQueue.push(e); });        // 不 await 慢 I/O
  pi.on("tool_result", (e) => { auditQueue.push(e); });
  pi.registerTool(orderQuery);
};

session.dispose(); // 同步；runtime.dispose() 是 async 必须 await
```

**Provider**：`openai-completions` + `baseUrl` + `apiKey: "$OPENAI_API_KEY"`（Bearer 自动携带）；DeepSeek 类兼容端点按需 `compat: { supportsDeveloperRole:false, supportsStore:false, maxTokensField:"max_tokens", supportsReasoningEffort:false }`。

## 附录 B：基础设施契约（迁移期不变）

- Redis 7（AOF）：短程记忆 `smartcs:short_term:{session}`、WorkingSetCache（**session_state 的 pending_action 迁至 MySQL**，§5.4）。
- MySQL 8（:3307）：conversation_session(+harness_version)、agent_run_receipt、memory_source_event、pending_action、用户记忆队列、audit。
- SQLite（`data/orders.db`）：订单/退款/ExecutionLedger/审批。
- RAG artifacts：`RAG_INDEX_ROOT=./artifacts/rag_jieba/production_indexes`（只读挂载），`RAG_SPARSE_MODE=global_corpus_v1`。
- LLM：`OPENAI_BASE_URL` + `OPENAI_API_KEY` + `MODEL_NAME`，temperature=0（**以 python-impl/.env 实际值为准**：Moonshot `kimi-k2.7-code`；原 v1 假设的 deepseek-v4-flash 系笔误来源，D2 裁决以 .env 为权威——Phase 0 已用真实端点冒烟通过）。
- Auth：JWT HS256，`AUTH_JWT_SECRET` ≥32B，HttpOnly+SameSite=strict，身份永不取自请求体；新增 `INTERNAL_SERVICE_JWT_SECRET`（独立密钥）。
- 新增 env：`SMARTCS_RUNTIME_CWD` `SMARTCS_PI_SESSION_DIR` `SMARTCS_PI_AGENT_DIR` `PYTHON_INTERNAL_BASE_URL`。
- OTel：`OTEL_EXPORTER_OTLP_ENDPOINT`；TS 侧接同一 collector。

## 附录 C：面试叙事（重构完成后）

> SmartCS 是基于 Pi Agent Harness 重构的智能客服业务 Agent。Pi 负责模型循环、会话、上下文压缩、工具选择和事件流；订单、退款、工单和 RAG 作为业务工具接入 Python Runtime。为避免模型直接控制真实业务状态，Agent decision 与 side-effect execution 分离：所有写操作经过可信授权（确认匹配在 Python 确定性服务）、ToolExecutor、幂等账簿和崩溃恢复。即使模型重试、Node 崩溃或 session 恢复，也不会因 transcript 不一致造成重复退款。

