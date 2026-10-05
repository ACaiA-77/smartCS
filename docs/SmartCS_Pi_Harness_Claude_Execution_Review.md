# SmartCS × Pi Agent Harness 重构：对 Claude Code v1 计划的修订意见与执行版方案

> **用途**：本文件用于直接交给 Claude Code，作为对原《SmartCS × pi-agent 底座迁移计划（评估稿 v1）》的架构复核、问题修正和后续执行依据。  
> **目标不是重新讨论“要不要迁”**。迁移方向已确定：借此次重构学习 Pi 的 Harness 架构，并将 SmartCS 重构成一个在面试中更有技术含量、边界更清晰、故障语义更完整的 Agent 系统。  
> **核心要求**：学习 Pi，但不能为了“用了 Pi”而牺牲 SmartCS 已经具备的用户隔离、显式确认、幂等、崩溃恢复、RAG 评测和业务状态一致性。

---

## 0. 最终结论

### 0.1 迁移结论

**继续迁。**

但将迁移目标明确为：

> **用 Pi 替换 SmartCS 自研的 Agent Harness 通用层；保留 Python 侧业务执行、安全与数据资产。**

最终系统应是：

```text
Pi = Agent Runtime / Harness
Python = Business Runtime
```

Pi 负责：

- Agent loop
- LLM 调用
- Tool selection
- Agent transcript
- Context compaction
- Skills / system prompt
- Extension hooks
- SSE 生命周期事件
- Tool 调用生命周期
- Agent runtime observability

Python 保留：

- RAG 检索与 rerank
- 订单 / 退款 / 工单 domain
- ToolExecutor
- READ/WRITE 风险语义
- 显式确认的业务约束
- ExecutionLedger
- ExecutionReconciler
- Approval
- 用户长期记忆
- 用户 / 会话归属数据
- 业务 pending state
- 业务数据库

### 0.2 不要再把迁移描述成“把 Python 客服系统改写成 TypeScript”

准确表述应为：

> **SmartCS 从自研编排器迁移到 Pi Agent Harness，形成 Node/TS Agent Runtime + Python Business Runtime 的双层架构。**

### 0.3 不采用“多 Agent 化”作为重构目标

目标形态：

```text
一个 SmartCS Main Agent
+
一组确定性业务 Tools
+
一组 Guards / Extensions
+
一个独立 Business Runtime
```

不要为了显得复杂重新制造：

```text
IntentAgent
RefundAgent
TicketAgent
ComplianceAgent
OrderAgent
...
```

其中只有真正具备独立模型循环、上下文和自主工具决策的组件才称 Agent。

---

# 1. 对原 v1 计划的总体判断

原计划的主方向正确，以下部分保留：

1. 新建 Node/TS `pi-harness`。
2. Python RAG / ToolExecutor / Ledger / Domain Service 不重写。
3. 一期优先 HTTP 薄壳工具，不急着 MCP 化。
4. 单 Main Agent 优先，不默认上 Subagent。
5. Pi 负责 Agent loop、事件、streaming、compaction。
6. Python 继续做业务副作用的最终安全边界。
7. 先 spike，再扩大迁移。

但以下问题在 v1 中没有真正闭环，**必须在正式编码前改掉**：

1. Q4 会话生命周期映射不完整。
2. Q8 的问题定义已经不符合本次迁移动机。
3. Pi Session 与 SmartCS 业务 Session 被过度等同。
4. 未定义同一 session 的单写者并发语义。
5. 未定义 `client_request_id` 在新 Harness 中的去重语义。
6. 未定义“WRITE 已成功但 Pi tool_result 尚未持久化就崩溃”的恢复路径。
7. 未定义“用户显式确认”如何成为可信输入，而不是让模型自己传 `confirmed=true`。
8. SSE token streaming 与最终 Compliance 审核存在直接冲突。
9. 把 Pi compaction 当作现有 2,700+ 行 Context Manager 的直接替代过于乐观。
10. RAG “检索器不变”不代表端到端 RAG 质量不变。
11. v1 的“按 intent 灰度”会导致同一 session 跨 Harness。
12. Phase 1 顺手拆 RAG 服务扩大了迁移变量，不应同时做。
13. Pi 默认资源发现 / 编码工具 / `~/.pi` 配置对多用户服务端存在额外攻击面。
14. 当前 Python `/api/tools/execute` 的客户入口实际上不能直接承接全部 WRITE 工具，需要真正的 internal service 通道。
15. Pi 0.83 skill、1.0.x coding-agent SessionManager、历史 agent-core harness v4 语义存在混用风险。
16. “Pi 自带 TUI”只能作为开发/调试工具，不能视为现有客服工作台的直接替代。

---

# 2. Q8：不再问“迁不迁”，改成“哪些东西不能迁”

## 2.1 决策

**迁移已经确定。**

原因不是商业 ROI，而是：

- 学习成熟 Agent Harness 的结构；
- 亲手理解 Agent loop、session、context、events、tools、extensions；
- 把 SmartCS 从“固定 async workflow + 一堆叫 Agent 的 Handler”升级成真正的 Harness 架构；
- 让面试时能够讲清楚：
  - Agent Runtime 和 Business Runtime 的边界；
  - Tool decision 和 Tool execution 的边界；
  - transcript、business state、side effect state 三种状态的区别；
  - crash recovery / idempotency / user confirmation；
  - compaction、memory、RAG、observability 如何接进 Agent Harness。

因此 Q8 应改成：

> **在确定迁移 Pi 的前提下，哪些能力必须留在 SmartCS 自己手中，而不能因为 Pi 已提供类似功能就迁掉？**

## 2.2 必须保留的差异化资产

以下内容**不交给 Pi 做最终权威**：

```text
ToolExecutor
ExecutionLedger
ExecutionReconciler
Approval
Business Pending State
User Ownership
Request Idempotency
RAG Retrieval Pipeline
Long-term User Memory
Domain Database
```

Pi 可以“知道”这些状态，但不能成为这些状态的最终权威。

## 2.3 面试价值

最终要能讲成：

> Pi 负责 Agent 的“思考与行动循环”，但它不拥有真实业务状态。模型决定是否调用退款工具，真正能不能退、是否已经退过、是不是本人、是否完成显式确认，由后端确定性 Runtime 决定。

这比“我用了 Pi 框架”更有价值。

---

# 3. Q4：会话生命周期必须重新定义

这是整个迁移最重要的设计点。

## 3.1 当前 SmartCS 不是一种 session 状态

现有系统至少包含以下状态：

### A. Platform Session

`conversation_session`

负责：

- `session_id`
- `account_id`
- session ownership
- title
- initial `client_request_id`
- created / updated time

它回答：

> **“这个会话是谁的？”**

### B. Conversation / Agent History

当前由：

- `conversation_event`
- checkpoint history
- Redis short-term history

共同承担。

它回答：

> **“这个客服会话之前发生过什么？”**

### C. Structured Business Conversation State

`ConversationState`

包含：

- `last_intent`
- `accumulated_entities`
- `turn_count`
- `pending_action`

它回答：

> **“当前业务交互处于什么状态？”**

### D. Workflow Checkpoint

`AgentCheckpoint`

包含：

```text
PREPARED
ROUTED
EXECUTING
GENERATED
REVIEWED
WAIT_CONFIRM
FINISHED
```

它回答：

> **“旧自研 workflow 跑到哪一步了？”**

### E. Request Receipt

当前 checkpoint request receipt 用于：

```text
(session_id, client_request_id)
```

请求级去重。

它回答：

> **“这个 HTTP/用户请求是不是已经处理过？”**

### F. Side-effect State

`ExecutionLedger`

回答：

> **“这次真正的 WRITE 是否已经发生？”**

这六类状态不能被一个 `SessionManager.inMemory()` 粗暴替代。

---

# 4. 重构后的四个权威状态

正式迁移后必须冻结下面的 Source of Truth。

## 4.1 Platform Session：MySQL 权威

保留：

```text
conversation_session
```

职责：

- session ID
- account ownership
- title
- harness version
- created / updated time

建议新增：

```text
harness_version = legacy | pi
```

一个 session 从创建开始就固定使用一个 Harness。

**禁止同一个 session 根据 intent 在 legacy / pi 之间来回切。**

---

## 4.2 Pi Transcript：TS / Pi Session Store 权威

新增独立 Pi session 持久化。

它负责：

- user message
- assistant message
- tool call/result transcript
- system prompt state
- tool declaration state
- compaction entries
- model / thinking state
- Pi custom entries
- Pi context edits
- active leaf

它回答：

> **“Pi 下一轮真正应该给模型什么上下文？”**

不要让 Python `conversation_event` 成为新 Pi session 的长期 canonical transcript。

否则会产生：

```text
Pi Entry
 → Python Event
 → Pi Entry
```

双向转换和语义漂移。

---

## 4.3 Business Interaction State：Python 权威

例如：

```text
pending_refund
selected_order
required_confirmation
approval state
```

必须留在 Python / MySQL。

Pi 可以每轮读取并注入，但不能把它只存在 transcript 里。

特别是：

```text
pending_action
```

不能只存：

```text
Pi custom entry
```

因为它属于业务事务前置状态。

---

## 4.4 Side-effect State：ExecutionLedger 权威

退款 / 工单创建等真实 WRITE：

```text
Python ToolExecutor
   ↓
ExecutionLedger
   ↓
Domain DB
```

Pi session 里的：

```text
tool_result
```

只是 Agent transcript。

它**不是**业务执行成功的最终凭证。

---

# 5. 新 Session ID 不再做二次映射

建议：

```text
SmartCS session_id
==
Pi session_id
```

现有 UUID 可以直接作为 Pi session ID。

不要再维护：

```text
smartcs_session_id ↔ pi_session_id
```

映射表。

创建新 session：

```text
Platform DB:
create conversation_session(id=UUID, harness_version="pi")
```

Node：

```ts
SessionManager.inMemory(
  SMARTCS_RUNTIME_CWD,
  { id: sessionId }
)
```

---

# 6. Pi Session 的持久化方案

## 6.1 禁止把 Node 内存对象当持久层

`AgentSession` 只是：

> **活跃运行实例。**

可以被：

- idle 回收
- Node restart
- deploy
- crash

随时销毁。

### 建议数据模型

可以设计：

```text
pi_session
----------
session_id
account_id
revision
active_leaf_id
pi_sdk_version
created_at
updated_at

pi_session_entry
----------------
session_id
seq
entry_id
entry_json
created_at
UNIQUE(session_id, seq)
UNIQUE(session_id, entry_id)
```

也可以先采用单 blob snapshot + revision 完成 spike。

重点不是具体表结构，而是必须满足：

```text
外部持久化
+
revision
+
原样保存 Pi entries
```

不要把 Pi entry 自己拆成 SmartCS 自定义 message schema。

---

# 7. Idle 回收的正确语义

Idle 回收只做：

```text
active AgentSession
        ↓
flush durable entries
        ↓
session.dispose()
        ↓
从 Node SessionRegistry 删除
```

它**不是删除会话**。

用户下次从另一设备回来：

```text
session_id
 ↓
JWT 校验
 ↓
ownership 校验
 ↓
读取 Pi entries
 ↓
SessionManager.inMemory(..., entries)
 ↓
createAgentSession()
 ↓
继续对话
```

---

# 8. 必须实现“一 session 单写者”

这是 Claude v1 没有解决的问题。

场景：

```text
PC:
“帮我退款”

Phone:
“先别退”
```

如果 Node A / Node B 同时 hydrate 相同 session：

```text
revision 18
```

然后都开始 Agent loop，会产生：

```text
两个 Agent
两个 tool decision
两个 transcript branch
甚至两个 WRITE 尝试
```

即使 ExecutionLedger 能防重复副作用，会话本身也可能乱序。

## 8.1 必须满足

```text
同一 session 在任意时刻只有一个 canonical turn writer
```

建议：

### Node 内

```text
SessionRegistry
session_id -> mutex / actor queue
```

同一 session 的请求串行。

### 跨 Node

使用：

```text
session execution lease
```

实现方式可以复用现有 MySQL session lock 思路，或者新增 Redis / DB lease。

要求：

- lease 有 owner
- lease 有 timeout
- crash 后可回收
- 写 session entries 时仍做 revision CAS

不要只依赖 sticky session。

---

# 9. `client_request_id` 必须保留

Pi session 不能替代 request idempotency。

场景：

```text
用户发送“确认退款”
        ↓
处理成功
        ↓
浏览器没收到响应
        ↓
重发同一个 request
```

如果只恢复 Pi transcript，仍可能重复：

```text
session.prompt("确认退款")
```

所以新 Harness 必须保留：

```text
session_id
client_request_id
request_hash
status
response
```

建议新增：

```text
agent_run_receipt
-----------------
session_id
client_request_id
request_hash
status = processing | completed | failed_recoverable
response
pi_revision_before
pi_revision_after
created_at
updated_at
```

请求进入先做：

```text
1. ownership
2. request receipt lookup
3. same ID + same hash + completed
   → 直接 replay response
4. same ID + different hash
   → 409
5. processing
   → 进入恢复逻辑，不重新盲跑 prompt
```

---

# 10. Claude v1 漏掉的最重要故障：WRITE 成功但 Pi transcript 未完成

必须设计以下故障：

```text
LLM
 ↓
refund_create tool call
 ↓
Python ToolExecutor
 ↓
退款成功
 ↓
ExecutionLedger 已记录
 ↓
Node 在写入 Pi tool_result / final answer 前崩溃
```

这时真实世界：

```text
退款已经发生
```

但 Pi transcript 可能只知道：

```text
“我要调用 refund_create”
```

甚至不知道最终结果。

如果恢复后让 Agent 自由重跑：

```text
可能再次调用 refund_create
```

Ledger 虽然会拦住重复副作用，但 Agent transcript 会出现异常、重复调用甚至错误回答。

---

# 11. WRITE Tool 必须使用可恢复 Operation ID

所有 WRITE Tool wrapper 在调用 Python 前生成：

```text
operation_id
```

要求：

```text
operation_id
```

在同一个用户 request 的重试/恢复中稳定。

例如：

```text
hash(
  session_id
  + client_request_id
  + tool_name
  + canonical_business_target
)
```

或由 Python `prepare` 阶段返回。

Python：

```text
ToolExecutor
 ↓
ExecutionLedger(operation_id/idempotency_key)
```

TS 的 request receipt 同时记录：

```text
open_write_operations[]
```

如果 Node crash：

```text
Node B
 ↓
发现 request receipt = processing
 ↓
发现 operation_id
 ↓
调用 Python reconcile/status
 ↓
得到 authoritative write result
 ↓
恢复 agent run
```

**不要直接重新发一次未知状态的 WRITE。**

---

# 12. 显式用户确认不能让模型自己“证明”

这是 v1 计划里非常关键但没有展开的信任边界。

错误设计：

```text
refund_create({
  confirmed: true
})
```

如果 `confirmed` 是 LLM 参数：

> 模型可以自己生成 `true`。

那么“显式用户确认”失去意义。

## 12.1 正确设计

用户原始消息：

```text
“确认退款”
```

先由 Harness 的确定性层判断：

```text
trusted_turn_context.user_confirmed = true
```

这个字段来自：

```text
raw user input
+
existing pending action
```

而不是来自模型。

Tool wrapper 调 Python 时：

```text
confirmed = trusted_turn_context.user_confirmed
```

模型不能覆盖。

## 12.2 更推荐的 Tool API

不要把低级：

```text
refund_create(order_id, reason, confirmed)
```

直接暴露给模型。

更推荐：

```text
refund_evaluate(order_id, reason)
    ↓
Python 创建 pending_action_id
    ↓
返回：
pending_action_id
amount
summary
confirmation_required=true
```

用户显式确认以后：

```text
refund_confirm(pending_action_id)
```

Python 再检查：

- pending action owner
- session owner
- action 未过期
- order 状态
- trusted confirmation
- idempotency
- ledger

这样比让模型重新拼退款参数安全很多。

---

# 13. 当前 `/api/tools/execute` 不能直接作为全部内部工具通道

当前 Python API 的 customer tool argument guard 只允许客户直接调用部分 READ tools。

WRITE tools 并不是简单通过现有公网接口就能调用。

因此 Claude 的：

```text
TS defineTool
→ existing /api/tools/execute
```

只能作为概念图。

正式实现应新增：

```text
/internal/tools/execute
```

或等价内部 service endpoint。

## 13.1 Internal endpoint 要求

使用单独鉴权：

```text
Internal Service JWT
```

claim 至少包含：

```text
aud = smartcs-business-runtime
account_id
business_user_id
session_id
client_request_id
iat
exp
```

Python 必须：

1. 验证 service JWT。
2. 再验证 session ownership。
3. 不信任 LLM 提供的 user_id。
4. 不允许模型直接设置 confirmed / approval。
5. 仍然统一经过 ToolExecutor。

公网 `/api/tools/*` 的权限语义不要为了 Pi 改坏。

---

# 14. Node HTTP Tool wrapper 不负责业务重试

TS 薄壳不要再实现第二套：

```text
retry policy
```

原则：

```text
Agent/HTTP adapter:
transport only

Python ToolExecutor:
business retry authority
```

特别是 WRITE：

```text
Node 不做自动 retry
```

READ 是否重试也尽量由 Python ToolExecutor 控制，避免形成：

```text
Node retry × Python retry
```

乘法重试。

---

# 15. Cancellation 不等于 WRITE 失败

SSE disconnect：

```text
req.close
 ↓
session.abort()
 ↓
HTTP AbortSignal
```

只能说明：

```text
Node 不再等待
```

不能说明：

```text
退款一定没有发生
```

如果 Python 已经开始执行 WRITE：

```text
client abort
```

不能把结果简单标成：

```text
failed
```

必须视为：

```text
UNKNOWN
```

恢复时：

```text
operation_id
 ↓
ExecutionLedger / Reconciler
 ↓
最终确定 completed / not-executed
```

这是必须进入故障注入测试的场景。

---

# 16. Compliance 与 SSE：不能“先流出去，再审核”

当前 v1 方案：

```text
message_update.text_delta
→ 直接 SSE 给用户
```

然后最后：

```text
agent_before_settle
→ Compliance
```

这是不成立的。

如果模型已经输出 PII / 禁止承诺：

```text
SSE 已经发给用户
```

最后再替换 final message 没有意义。

## 16.1 一期正确方案

SSE 分成两种 channel：

### 可实时发

```text
status
tool_status
progress
thinking-disabled public status
```

例如：

```text
正在查询订单
正在检查退款资格
正在检索知识库
```

### 最终答案

```text
LLM output
 ↓
server buffer
 ↓
Compliance rule
 ↓
LLM compliance review
 ↓
pass / sanitize
 ↓
SSE final content
```

一期不要做未经审核的 token-by-token final answer。

之后若真要 token streaming，再专门设计：

```text
incremental safety filter
```

不要在本次迁移里一起做。

---

# 17. Context：不要让 Pi Compaction 吃掉业务状态

Pi compaction 适合管理：

```text
conversation transcript
```

但不能承担：

```text
pending refund
current order
user ownership
approval state
idempotency state
```

这些必须存结构化业务状态。

推荐：

```text
Pi Session
负责 transcript compaction

Python Context Service
负责结构化业务上下文
```

每轮：

```text
Business Context Snapshot
-------------------------
user profile
pending action
important entities
recent verified order facts
long-term memory highlights
```

通过 extension / context injection 注入。

即使 Pi 把历史 transcript 压缩掉：

```text
关键业务状态仍可重新注入
```

---

# 18. 现有 2,700+ 行 Context Manager 不要一次性全删

v1 把：

```text
现有 context assembler
→ Pi compaction
```

想得太简单。

建议拆成两类：

## A. 交给 Pi

- transcript
- context window
- generic compaction
- system prompt
- skills
- recent tool conversation

## B. 保留 / 简化成 Python Context Service

- user memory selection
- protected business fields
- entity snapshot
- pending state
- authoritative recent order facts
- domain-specific token budget
- redaction

迁移目标应是：

> **缩小 Context Manager 的职责，而不是第一天删除。**

---

# 19. Memory I/O 不建议直接塞进 `pi.on` 做慢调用

`pi.on` hook 会影响 Agent loop 生命周期。

不要在每一个 hook 里：

```text
await MySQL
await Python memory service
await network
```

建议 request handler 在 `session.prompt()` 前：

```text
parallel:
- hydrate Pi session
- load business state
- load user memory snapshot
```

得到：

```text
TurnContext
```

extension 只同步/轻量地注入已经准备好的 snapshot。

这会让：

- 延迟更可控；
- hook 更纯；
- 测试更简单；
- trace 更清楚。

---

# 20. RAG 不能只验证 Recall/MRR/nDCG 不变

Retriever 不改：

```text
≠
端到端 RAG 不改
```

旧链路可能是：

```text
Intent
→ Query Rewrite
→ Retriever
→ Evidence
→ RAG Prompt
→ Answer
```

Pi 后：

```text
Main Agent
→ 决定是否调用 knowledge_search
→ 决定 query
→ 读取 evidence
→ Main Agent answer
```

变化包括：

- 会不会调用搜索；
- 什么时候调用；
- 搜什么 query；
- 是否重复搜索；
- 是否真正引用检索证据；
- 最终 answer prompt 已经变化。

## 20.1 一期保持旧 Query Rewrite

不要为了 Pi 同时删除 RAG 逻辑。

一期：

```text
knowledge_search(query)
 ↓
Python 内部继续使用现有 rewrite/retriever/rerank
```

后续再单独实验：

```text
Main Agent query
vs
RAG rewrite query
```

## 20.2 增加 E2E RAG Gate

继续保留：

```text
Recall@10
MRR@10
nDCG@10
wrong-domain
```

并新增：

```text
retrieval-needed accuracy
tool invocation rate
unnecessary retrieval rate
answer groundedness
evidence usage
citation correctness
```

---

# 21. IntentRouter 不要原样搬到 TS

如果迁完还是：

```text
Intent LLM
 ↓
route
 ↓
Main LLM
 ↓
Tool selection
```

就仍然是两次重复决策。

## 21.1 推荐

Main Agent 固定拥有少量安全 READ tools：

```text
knowledge_search
order_query
refund_evaluate
ticket_query
risk_check
```

直接由模型选择。

Intent classifier 只负责：

- metrics
- UI label
- eval bucket
- fallback / guard
- legacy comparison

不再作为主业务路由器。

## 21.2 WRITE Tool

WRITE 不靠 intent whitelist 保安全。

依靠：

```text
trusted turn state
+
pending business state
+
ToolExecutor
```

保安全。

---

# 22. 不上 Subagent 作为第一阶段目标

当前业务规模不需要：

```text
每 intent 一个 Subagent
```

先实现：

```text
SmartCS Main Agent
```

如果后续 eval 证明：

- RAG prompt 与交易 prompt 严重冲突；
- tool 数量扩大；
- 上下文隔离确实带来收益；

再考虑专业 Subagent。

不要为了面试“看起来复杂”提前上多 Agent。

一个边界清楚的 Harness 比伪多 Agent 更加分。

---

# 23. Pi 服务端必须关闭编码助手的默认产品烙印

这不仅是“换 system prompt”。

生产客服 Harness 应：

1. 禁止默认 `read/bash/edit/write`。
2. 不加载用户 home 下非预期资源。
3. 不允许任意 cwd。
4. 不允许客户输入影响 extension path / skill path。
5. 不自动扫描非白名单项目资源。
6. 使用固定 runtime cwd。
7. 使用显式 ResourceLoader / SettingsManager。
8. 只加载 SmartCS 自己允许的：
   - system prompt
   - skills
   - extensions
   - business tools

建议使用接近 H01 full-control 的方式初始化 Server Harness。

不要让：

```text
~/.pi
.pi/
AGENTS.md
random project extension
```

意外改变线上 Agent 行为。

---

# 24. Tool Result 要防止 Context Pollution 和 Tool-output Prompt Injection

尤其：

```text
knowledge_search
```

返回的是外部/知识库文本。

模型不应把文档中的：

```text
“忽略系统指令”
```

当作新指令。

要求：

- tool result 明确标记为 untrusted data；
- system prompt 明确规定工具结果只是数据；
- RAG evidence 用结构化 delimiter；
- 业务 tool content 返回最小必要文本；
- 大量结构化细节优先放 program-only metadata；
- 限制单次 tool result 最大大小；
- 对 tool 输出做 schema 校验。

这是 Harness 层必须有的安全设计。

---

# 25. Observability 必须统一跨 TS → Python

每轮至少传播：

```text
trace_id
session_id
client_request_id
agent_run_id
tool_call_id
operation_id
```

TS：

```text
Pi agent span
  ├─ model span
  ├─ tool_call span
  └─ compliance span
```

Python：

```text
ToolExecutor span
  ├─ ledger span
  ├─ domain span
  └─ DB span
```

使用 W3C `traceparent` 或等价统一 trace context。

这样面试时可以直接展示：

> 一次 Agent turn 如何跨 Node Harness 和 Python Business Runtime 全链路追踪。

---

# 26. 新旧 Conversation Event 不要长期双写

Pi 新 session 上线以后：

## Legacy session

继续：

```text
CheckpointStore
conversation_event
legacy workflow
```

## Pi session

使用：

```text
Pi transcript store
```

Python 只保留：

```text
business/audit events
execution ledger
```

不要长期把所有 Pi message 再翻译一份写进旧 `conversation_event`。

否则会出现两个 conversation source of truth。

---

# 27. 旧 CheckpointStore 要拆职责，而不是一句“保留/删除”

旧 Checkpoint 实际承担：

```text
A. workflow stage
B. session lock
C. request dedupe
D. event history
E. digest
```

迁移后：

### 删除

```text
PREPARED
ROUTED
EXECUTING
GENERATED
REVIEWED
```

这种旧 Workflow stage。

### 迁移 / 保留

```text
session lock
request receipt
```

### 替换

```text
conversation event history
→ Pi Session Store
```

### 重新评估

```text
digest
→ Pi compaction + business context snapshot
```

不要把 `ExecutionLedger` 当作 Checkpoint 的替代品。

两者解决的问题不同。

---

# 28. 灰度不能按 Intent

原：

```text
conversation / RAG → Pi
refund → legacy
```

会导致同一用户同一 session：

```text
第一轮旧 Harness
第二轮 Pi
第三轮旧 Harness
```

这是错误设计。

## 正确方法

创建 session 时：

```text
harness_version = legacy | pi
```

整个 session 固定。

灰度方式：

```text
hash(account_id)
```

或：

```text
new session cohort
```

例如：

```text
5% 新 session → Pi
95% 新 session → Legacy
```

旧 session 永远保持原 Harness。

---

# 29. Phase 1 不要同时拆 RAG 微服务

迁 Harness 已经有足够变量。

一期：

```text
Pi Harness
 ↓ HTTP
现有 Python Process
 ↓
RAG / Domain
```

即可。

不要同时：

```text
Python API
→ 新拆 RAG service
```

RAG 服务化是二期独立重构。

---

# 30. Pi 版本策略重新冻结

不要把：

```text
dg-piagent 0.83 skill
agent-core 历史 harness v4
coding-agent 1.0.x SessionManager
```

混成一种 Session API。

执行原则：

1. 开工当天确认当前 Pi release。
2. 选择一个确切 patch version。
3. `package-lock.json` 锁死。
4. 只以该版本：
   - `dist/**/*.d.ts`
   - `examples/sdk`
   - package docs
   - CHANGELOG
   为 API 真相。
5. 更新 `dg-piagent` skill 到该版本以后再正式编码。
6. 迁移过程中禁止自动升级 Pi。

当前审阅时官方 changelog 已出现 1.0.1，因此原计划固定 1.0.0 需要重新核验后再冻结。

---

# 31. 推荐的最终架构

```text
                         Web / Client
                              │
                              │ JWT
                              ▼
                    ┌────────────────────┐
                    │   Pi Harness API   │
                    │    Node / TS       │
                    └─────────┬──────────┘
                              │
                   ownership / request receipt
                              │
                 ┌────────────▼────────────┐
                 │ Session Runtime Registry│
                 │ local actor + lease     │
                 └────────────┬────────────┘
                              │
                    hydrate Pi entries
                              │
                 ┌────────────▼────────────┐
                 │   SmartCS Main Agent    │
                 │       Pi Agent          │
                 │                         │
                 │ Agent Loop              │
                 │ Session / Compaction    │
                 │ Tools                   │
                 │ Extensions              │
                 │ Events                  │
                 └───────┬─────────┬───────┘
                         │         │
                 READ tools     WRITE tools
                         │         │
                         └────┬────┘
                              │
                 signed internal request
                              │
                ┌─────────────▼──────────────┐
                │ Python Business Runtime    │
                │                            │
                │ Internal Tool Gateway      │
                │ ToolExecutor               │
                │ ExecutionLedger            │
                │ ExecutionReconciler        │
                │ Approval                   │
                │ Pending Business State     │
                └──────┬────────┬────────────┘
                       │        │
                ┌──────▼───┐ ┌──▼────────┐
                │   RAG    │ │  Domains   │
                │ Retrieval│ │Order/Refund│
                │ Rerank   │ │Ticket      │
                └──────────┘ └────────────┘
```

独立持久化：

```text
MySQL
├─ platform_session
├─ pi_session
├─ pi_session_entry
├─ agent_run_receipt
├─ business pending state
├─ user memory
└─ audit

SQLite / domain storage
├─ order
├─ refund
└─ ExecutionLedger
```

---

# 32. 重写后的实施阶段

## Phase 0 — Version / Runtime Spike

只验证 Pi 自身。

完成：

- 锁定 Pi exact version；
- Node runtime version；
- DeepSeek/OpenAI-compatible provider；
- custom system prompt；
- 禁用所有 coding built-in tools；
- 注册 2 个 fake read tools；
- `session.subscribe`；
- extension `pi.on`；
- abort；
- inMemory session hydrate / rehydrate；
- minimal SSE。

验收：

```text
PASS:
Pi agent loop 能运行
session 能外部恢复
tool_call hook 正常
无默认 bash/read/write/edit
SSE 生命周期正常
```

不要接真实 refund。

---

## Phase 1 — Session Foundation

这是原计划缺失的阶段。

实现：

- `harness_version`
- Pi transcript store
- `agent_run_receipt`
- SessionRegistry
- same-session local queue
- cross-node session lease
- revision CAS
- idle eviction
- cross-device resume
- request dedupe

验收必须模拟：

```text
Node restart
Node crash
两个设备同时发消息
相同 request_id 重发
不同 payload 复用 request_id
idle dispose 后恢复
```

**Phase 1 不通过，禁止接业务 WRITE。**

---

## Phase 2 — Read-only Business Parity

工具：

```text
knowledge_search
order_query
ticket_query
risk_check
refund_evaluate
```

其中：

```text
refund_evaluate
```

仍是 READ。

实现：

- internal service auth
- user/session identity envelope
- tool schema
- Python internal gateway
- Main Agent tool selection
- RAG 保持现有内部实现
- intent classifier 降级为 observability label

验收：

- 无越权查询；
- user_id 不能由模型控制；
- read tool parity；
- RAG 检索 benchmark 不回归；
- E2E retrieval/tool choice eval。

---

## Phase 3 — Context / Memory / Compliance

实现：

- Pi transcript compaction；
- Python Business Context Snapshot；
- user memory prefetch；
- context injection；
- compliance rule；
- compliance LLM review；
- final response buffer；
- SSE status stream。

验收：

- compaction 后 pending business facts 不丢；
- memory 不串用户；
- 未审核 final token 不外发；
- PII 用例不能先流出再被替换。

---

## Phase 4 — WRITE Shadow Mode

先让 Pi 产生：

```text
write intent / tool plan
```

但不实际执行。

对比 Legacy：

```text
是否选择 refund
是否选择 ticket
参数是否一致
是否要求 confirmation
```

验收达到阈值后再打开真实 WRITE。

---

## Phase 5 — WRITE Enable + Recovery

实现：

```text
refund_evaluate
→ pending_action
→ explicit user confirmation
→ refund_confirm
```

以及：

```text
ticket_create
```

必须加入：

- trusted confirmation；
- operation_id；
- request receipt；
- unknown outcome reconcile；
- Python ledger；
- crash recovery。

---

## Phase 6 — Observability

完成：

```text
TS Pi span
→ HTTP trace context
→ Python ToolExecutor span
→ Domain / Ledger
```

统一：

```text
session_id
request_id
tool_call_id
operation_id
```

---

## Phase 7 — Cohort Rollout

新增 session 才参与灰度。

```text
conversation_session.harness_version
```

决定整段 session 使用哪个 Harness。

禁止按 intent 切。

---

## Phase 8 — Optional MCP

HTTP thin tools 稳定后再评估 MCP。

优先考虑 READ tools。

WRITE tools 是否 MCP 化不作为本项目成功条件。

---

# 33. 必须增加的故障注入验收矩阵

Claude 执行时必须有自动化或至少可重复的 integration tests。

## F1 Node 在 LLM 前崩溃

预期：

```text
无 tool side effect
request 可安全恢复
```

## F2 READ tool HTTP 中断

预期：

```text
可以重新执行
无业务副作用
```

## F3 WRITE 请求发到 Python 前 Node 崩溃

预期：

```text
Ledger 无记录
恢复后可重新执行
```

## F4 Python WRITE 成功、响应返回前连接断

预期：

```text
operation = UNKNOWN
reconcile ledger
不能盲 retry
```

## F5 Python WRITE 成功、TS 收到结果，但 Pi tool_result 未落盘

预期：

```text
恢复 request receipt
reconcile operation
不能重复真实 WRITE
transcript 能恢复为一致状态
```

## F6 final answer 生成后、receipt completed 前 Node 崩溃

预期：

```text
不能再次执行 WRITE
恢复后重新生成或 replay final response
```

## F7 用户重复提交同 client_request_id

预期：

```text
不重复 Agent turn
不重复 tool side effect
```

## F8 两设备同时向同 session 发消息

预期：

```text
严格排序
无双 Agent writer
```

## F9 SSE 客户端断开时 WRITE 正在执行

预期：

```text
不能把 abort 当作业务失败
重新连接后可查询真实状态
```

## F10 Pi compaction 后再确认退款

预期：

```text
pending action 仍来自 Python structured state
不依赖旧 transcript 文本
```

## F11 Tool result 中包含 prompt injection

预期：

```text
模型把它当 data
不能修改系统安全策略
```

## F12 Node A crash，Node B 恢复 session

预期：

```text
同 session_id
同 transcript
同 business state
同 request dedupe 语义
```

---

# 34. 测试指标不能只看“功能跑通”

建议最终迁移 Gate：

## Agent Runtime

- turn success rate
- tool selection accuracy
- average tool calls / turn
- unnecessary tool call rate
- P50 / P95 latency
- token usage
- context compaction frequency

## RAG

- Recall@10
- MRR@10
- nDCG@10
- wrong-domain
- retrieval-needed accuracy
- grounded answer rate

## Business Safety

- unauthorized tool execution = 0
- WRITE without trusted confirmation = 0
- duplicate side effect = 0
- cross-user leakage = 0
- request replay duplication = 0

## Recovery

- F1–F12 故障注入全过

---

# 35. Claude Code 执行时的硬约束

以下规则不要自行改：

1. **不要重写 RAG。**
2. **不要重写 ToolExecutor。**
3. **不要删除 ExecutionLedger / Reconciler。**
4. **不要让 Pi transcript 成为业务退款状态权威。**
5. **不要让模型提供 trusted user identity。**
6. **不要让模型提供 trusted confirmation。**
7. **不要让 Node thin tool 自己实现 WRITE retry。**
8. **不要按 intent 灰度。**
9. **不要 Phase 1 顺手拆 RAG service。**
10. **不要默认开启 read/bash/edit/write。**
11. **不要使用用户 home `.pi` 资源作为线上 Agent 行为来源。**
12. **不要把 Pi agent-core 历史 Harness API 和当前 coding-agent SDK SessionManager 混用。**
13. **不要一开始做 Subagent。**
14. **不要为了 MCP 而 MCP。**
15. **不要在 Compliance 前把 final answer token 直接流给用户。**
16. **不要长期双写两套 conversation transcript。**

---

# 36. 建议目录边界

建议新增：

```text
pi-harness/
├─ src/
│  ├─ agent/
│  │  ├─ create-smartcs-agent.ts
│  │  ├─ prompt/
│  │  ├─ tools/
│  │  └─ extensions/
│  ├─ session/
│  │  ├─ registry.ts
│  │  ├─ repository.ts
│  │  ├─ lease.ts
│  │  └─ receipt.ts
│  ├─ business/
│  │  ├─ python-client.ts
│  │  ├─ identity.ts
│  │  └─ contracts.ts
│  ├─ streaming/
│  ├─ tracing/
│  └─ server/
└─ tests/
```

Python 新增尽量控制为适配层：

```text
internal_api/
├─ auth.py
├─ tools.py
├─ context.py
├─ memory.py
└─ operation_status.py
```

不要大规模移动现有业务模块。

---

# 37. 推荐 Tool 边界

第一版 Main Agent 暴露：

```text
knowledge_search
order_query
ticket_query
refund_evaluate
refund_confirm
ticket_create
risk_check
```

其中：

```text
refund_confirm
ticket_create
```

属于 WRITE。

比直接暴露低级：

```text
refund_create
```

更适合 Agent Tool 语义。

低级 domain write 可以继续存在于 Python 内部：

```text
refund_confirm
  ↓
ToolExecutor
  ↓
refund_create domain operation
```

---

# 38. 面试时最终项目应该怎么讲

重构完成以后，不再说：

> “我做了一个多 Agent 客服系统，里面有 Intent Agent、Refund Agent、Ticket Agent……”

推荐：

> **SmartCS 是一个基于 Pi Agent Harness 重构的智能客服业务 Agent。Pi 负责模型循环、会话、上下文压缩、工具选择和事件流；订单、退款、工单和 RAG 作为业务工具接入 Python Runtime。为了避免模型直接控制真实业务状态，我把 Agent decision 和 side-effect execution 分开：所有写操作仍经过显式用户确认、ToolExecutor、幂等账簿和崩溃恢复。**

如果继续追问：

> “为什么还要保留 Python？”

回答：

> **因为 Harness 负责的是 Agent 怎么思考和调用工具，业务 Runtime 负责的是工具到底能不能执行。把这两个边界拆开以后，即使模型重试、Node 崩溃或者 session 恢复，也不会因为 Agent transcript 不一致造成重复退款。**

这就是此次重构最应该体现的技术价值。

---

# 39. Claude Code 下一步任务

不要直接开始全量迁移。

先按以下顺序执行：

### Task 1 — 修订迁移设计

基于本文档更新原 v1 计划，明确：

- Source of Truth 表；
- Pi session persistence；
- SessionRegistry；
- session lease；
- request receipt；
- trusted confirmation；
- write operation recovery；
- compliance streaming；
- harness_version rollout。

### Task 2 — Pi version audit

安装前：

- 核对当前稳定 patch；
- 对照当前 `dg-piagent` skill；
- 查看目标版本 `CHANGELOG` / `d.ts` / SDK examples；
- 输出 API delta；
- 冻结 exact version。

### Task 3 — Phase 0 Spike

只实现：

```text
Pi session
+
fake/read-only tool
+
external entries persistence/restore
+
SSE
+
abort
+
extensions
```

不要接 WRITE。

### Task 4 — Phase 1 Session Foundation

只有 Session / Receipt / Lease / Restore 的故障测试全部通过以后，才进入业务迁移。

---

# 40. 最终验收原则

这次重构成功，不是因为代码里出现：

```text
createAgentSession()
```

而是因为最终能清楚证明：

```text
模型决策
≠
业务执行

Agent transcript
≠
业务状态

Agent session recovery
≠
side-effect recovery

LLM confirmation interpretation
≠
trusted user confirmation

Pi compaction
≠
business state persistence
```

并且：

```text
Node 崩溃
网络重试
跨设备续聊
并发请求
工具超时
WRITE unknown outcome
```

这些情况下都不会破坏真实业务状态。

**请以这个标准执行 SmartCS × Pi 的重构。**
