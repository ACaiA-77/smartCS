# SmartCS Pi Harness 迁移收口问题与优化建议

> 目的：基于当前源码与实际运行结果，对 Pi Harness 迁移后的最终状态做一次收口评估。  
> 当前判断：**主体迁移已完成，不建议继续做大规模架构重构，也不建议为了统一语言将 Python 全量迁移到 TypeScript。后续重点应放在生产接线、接口边界、真实模型稳定性和文档一致性上。**

## P0：Memory Outbox 已实现，但生产入口未接通

### 问题

当前已经实现：

```text
memory_source_event
→ agent_run_receipt.memory_enqueue_status
→ MemoryOutboxDispatcher
→ /internal/memory/enqueue
→ UserMemoryService
```

但实际生产入口：

```text
pi-harness/src/server/main.ts
```

只启动了 `AuditDispatcher`，没有实例化并启动 `MemoryOutboxDispatcher`。

实际查询当前开发库：

```text
agent_run_receipt:
completed + memory_enqueue_status=pending = 18
```

即当前 18 条已完成 Pi 请求全部仍处于 `pending`，证明长期记忆写入链路目前没有真正闭环。

此外，现有 `MemoryOutboxDispatcher` 依赖：

```ts
identityFor(sessionId)
```

从内存中的 `TurnIdentity` 获取身份。

但请求结束以后 `TurnContext` 会被清空；Session idle eviction、Node 重启以后更不可能依赖内存恢复身份。

因此仅仅在 `main.ts` 中调用：

```ts
new MemoryOutboxDispatcher(...).start()
```

仍然不够。

### 优化建议

将 Memory Outbox 改造成**完全基于 durable state 的后台任务**。

推荐链路：

```text
请求完成
↓
agent_run_receipt
memory_enqueue_status=pending
↓
后台 Dispatcher 扫描
↓
根据 receipt / conversation_session / memory_source_event
恢复 account / business_user / session / request 身份
↓
生成内部 Service JWT
↓
POST /internal/memory/enqueue
↓
Python 再次校验 provenance
↓
UserMemoryService
↓
pending → done
```

核心原则：

> **Memory Outbox 的恢复不能依赖活着的 AgentSession、TurnContext 或任何 Node 进程内状态。**

同时补充以下验收：

- 正常请求完成后最终变成 `done`
- Node 在请求完成后、memory enqueue 前 kill -9，重启后仍可补投
- Session 已 idle eviction 后仍可补投
- 重复投递不产生重复 memory candidate
- 投递失败只重试 memory，不重放 LLM 和 Tool

---

## P0：System Prompt 与最终 Tool Surface 不一致

### 问题

当前 System Prompt 仍然写着：

```text
可用能力：
- order_query
- knowledge_search

你只能做只读查询。
```

但当前真实 Tool Surface 已经包括：

```text
order_query
knowledge_search
ticket_query
refund_evaluate
risk_check
refund_confirm
ticket_create
```

而且部署已经支持：

```text
SMARTCS_WRITE_MODE=live
```

因此 Prompt 与实际运行能力发生直接冲突。

真实模型可能因为系统提示中的“只能只读”而拒绝调用：

```text
refund_confirm
ticket_create
```

或者错误地告诉用户自己没有写操作能力。

### 优化建议

System Prompt 应根据实际 Tool Surface 重写。

建议明确：

```text
READ
- order_query
- knowledge_search
- ticket_query
- refund_evaluate
- risk_check

WRITE
- refund_confirm
- ticket_create
```

同时明确业务原则：

> Agent 可以提出 Tool 调用，但无权自行授权业务写操作。  
> WRITE 是否真正执行，由 Python 侧确定性的授权与执行层最终决定。

退款需要说明：

```text
refund_evaluate
→ 形成待确认退款
→ 用户明确回复确认
→ refund_confirm
```

工单则保持当前：

```text
用户本轮明确表达创建意图
→ ticket_create
```

不要在 Prompt 中让模型自行判断 `confirmed=true`。

---

## P1：模型可见 Tool Schema 暴露了不应由模型填写的系统字段

### 问题

当前模型 Tool Schema 中仍存在：

```text
user_id
client_request_id
request_payload_hash
```

例如：

```text
refund_evaluate.user_id
ticket_query.user_id
risk_check.user_id

ticket_create:
- client_request_id
- request_payload_hash
- user_id
- title
- description
...
```

Python Runtime 最终已经会：

```text
剥离模型传入的身份字段
↓
从可信 Service JWT 重新绑定 user_id
↓
自行生成 client_request_id / payload hash
```

因此这些参数对模型来说实际上都是“假参数”。

安全上目前不会因此越权，但会：

- 增加 Tool Schema 复杂度
- 增加模型漏参概率
- 增加模型生成错误 ID 的概率
- 让面试时职责边界显得不干净

### 优化建议

模型只应该看到它真正负责决定的业务参数。

例如：

```text
order_query
{
  order_id
}
```

```text
refund_evaluate
{
  order_id
}
```

```text
ticket_query
{
  ticket_id
}
```

```text
risk_check
{
  action,
  amount?
}
```

```text
ticket_create
{
  title,
  description,
  priority?,
  category?
}
```

运行时自己补：

```text
user_id
account_id
session_id
client_request_id
request_payload_hash
confirmed
operation_id
```

原则：

> **业务参数属于模型，身份、授权、幂等和追踪参数属于 Runtime。**

---

## P1：正式运行环境不应静默回退到 Faux Provider

### 问题

当前 Provider 配置缺失时：

```text
resolveProviderMode()
→ faux
```

这个行为对测试非常合理，但对正式运行不合适。

如果生产环境漏了：

```text
OPENAI_BASE_URL
OPENAI_API_KEY
MODEL_NAME
```

服务仍可能正常启动，只是 Agent 实际在返回 Faux 内容。

这样 `/health` 看起来正常，但服务实际上不可用。

### 优化建议

区分明确的运行环境：

```text
test / dev:
允许 SMARTCS_PHASE0_PROVIDER=faux

production:
模型配置缺失 → fail fast
```

建议 Faux 必须显式开启：

```text
SMARTCS_PROVIDER_MODE=faux
```

而不是“配置不完整自动 Faux”。

---

## P1：RAG 真实链路延迟需要继续优化

### 问题

当前 Pi → Python RAG 已经真实跑通，但真实检索存在约几十秒级耗时。

当前调用链包含：

```text
Agent 判断
↓
knowledge_search
↓
Dense
+
BM25
↓
RRF
↓
CrossEncoder
↓
Tool Result
↓
模型回答
```

功能上已经成立，但对于客服 Agent 来说，真实交互延迟仍然偏高。

### 优化建议

不要直接关闭 rerank 或降低检索质量，而是先把整条链拆开计时：

```text
query preparation
dense retrieval
BM25
RRF
CrossEncoder
serialization
Node ↔ Python transport
最终生成
```

为每一段记录：

```text
P50
P95
```

再定位真正瓶颈。

优先检查：

- CrossEncoder 是否每请求重复加载
- embedding / reranker 是否常驻
- FAISS / BM25 artifact 是否重复初始化
- RAG Tool 是否返回过多无用字段
- top-k → rerank-k 是否过大
- Query Rewrite 是否值得当前延迟成本

优化后重新跑现有 Recall / MRR / nDCG 基线，不能只追求速度。

---

## P1：最终架构文档严重滞后

### 问题

当前 `python-impl/README.md` 仍主要描述：

```text
ChatOrchestrator
IntentRouter
KnowledgeRAGAgent
TicketHandler
RefundHandler
```

甚至仍将项目描述为旧的“多 Agent 客服后端”。

但实际主架构已经变成：

```text
Pi Harness
↓
Main Agent
↓
Business Tools
↓
Python Runtime
↓
ToolExecutor / RAG / Domain
```

此外 `pi-harness` 当前没有完整 README。

部分历史 Phase Report 也保留了当时的 `blocked / deviation` 状态，但后续实际上已经完成。

### 优化建议

做一次文档收口，不再以 Phase 报告作为最终架构入口。

至少形成：

```text
README.md
docs/architecture.md
docs/runtime-boundaries.md
docs/recovery.md
```

最终 README 只描述当前架构，不再把旧 ChatOrchestrator 当主链路。

历史方案与 Phase 报告可以保留，但明确标记：

```text
Historical Migration Record
```

---

## P2：MCP 已接入，但需要明确“最终边界”

### 当前状态

Pi 1.0.1 原生 MCP 已经真实接入并通过测试。

当前：

```text
knowledge_search
→ Pi MCP Client
→ Streamable HTTP
→ Python MCP Gateway
→ RAG
```

身份绑定工具仍然走：

```text
Pi Tool
→ Internal HTTP
→ Python Runtime
```

包括：

```text
order_query
ticket_query
refund_evaluate
risk_check
refund_confirm
ticket_create
```

### 判断

**当前不需要为了“全 MCP”继续改造。**

共享知识库检索没有 per-user 身份，可以非常自然地使用 MCP。

而订单、退款、工单依赖：

```text
account_id
business_user_id
session_id
client_request_id
agent_run_id
tool_call_id
```

以及 WRITE authorization / idempotency / reconcile。

当前 HTTP envelope 对这些 per-turn 身份更清晰。

### 优化建议

保留当前混合方式即可：

```text
共享、无用户身份 Tool
→ MCP

强用户身份 / WRITE Tool
→ Internal HTTP
```

但建议在 Compose 增加一个明确的 MCP profile，使：

```text
SMARTCS_KNOWLEDGE_TRANSPORT=mcp
```

时可以一键启动 Python MCP Gateway。

这样项目不仅“代码支持 MCP”，还可以真实演示。

---

## P2：明确当前仅支持单 Pi Harness 实例

### 问题

当前同一 Session 的 single-writer 保证来自：

```text
SessionRegistry
→ Node 进程内 mutex
```

这对单实例完全成立。

但如果部署两个 Pi Harness 实例：

```text
Harness A
Harness B
```

两个进程之间没有共享 lease，同一 Session 仍可能出现双写 Pi Session 的问题。

### 优化建议

当前项目没有必要为了简历去实现 Redis / MySQL 分布式 Session Lease。

只需要明确边界：

> 当前 Pi Harness 按单实例运行，不声明支持 Harness 横向多副本。

如果未来真的需要多实例，再升级为：

```text
MySQL / Redis distributed lease
+ session routing
```

不要为了“架构高级”提前增加复杂度。

---

## P2：收口测试体系，避免不同测试文件相互污染

### 问题

本次审阅中同时运行多个依赖同一：

```text
smartcs_phase1_test
```

数据库的 Vitest 文件时，出现 Phase 7 403。

单独复跑：

```text
phase7-unified-entry.test.ts
→ 4/4 PASS
```

证明不是业务代码回归，而是多个测试文件会重置同一个测试数据库产生互相干扰。

另外 Python 与 TS 大测试并发执行时，由于 CrossEncoder 内存占用导致 Windows：

```text
os error 1455
页面文件太小
```

项目现有 Phase 报告本身也已经要求重型测试串行。

### 优化建议

正式固化测试纪律：

```text
integration / crash matrix
→ fileParallelism=false
→ 共用数据库的 suite 串行
```

最好进一步做到：

```text
每个 integration suite 独立 DB/schema
```

或者为测试生成唯一 database suffix。

CI 中不要同时启动 Python 大模型测试与 Pi 故障矩阵。

---

# 架构边界：不建议继续全量迁移到 TypeScript

当前不建议：

```text
Python → TypeScript 全重写
```

因为目前 Python 剩下的已经不是旧 Harness，而主要是：

```text
RAG
ToolExecutor
ExecutionLedger
WriteAuthorizationService
PendingAction
Order / Refund / Ticket Domain
Long-term Memory
Compliance
Business DB
```

这些模块和 Pi Harness 本身属于不同职责。

推荐保持：

```text
TypeScript / Pi
负责：
- Agent Loop
- LLM
- Session
- Context Compaction
- Tool Selection
- Skills
- MCP Client
- Streaming
- Agent Lifecycle
- Agent Observability
```

```text
Python
负责：
- RAG
- ToolExecutor
- Write Authorization
- ExecutionLedger
- Pending Action
- Order / Refund / Ticket
- Long-term Memory
- Business State
```

核心原则：

> **模型运行时可以替换，权威业务状态和副作用安全不能跟着 Harness 一起漂移。**

因此当前继续全 TS 重写的收益非常低，反而会重新引入已经解决过的幂等、恢复、RAG 与业务安全问题。

---

# 建议的最终收口顺序

优先只完成以下工作，不再新增大架构：

```text
1. 修 Memory Outbox 生产闭环
2. 重写最终 System Prompt
3. 精简 Tool Schema，删除模型不应填写的系统字段
4. Production 模式禁止 Faux 自动降级
5. 优化 RAG 延迟并保留质量回归
6. 更新 README / Architecture 到 Pi 最终态
7. 补 MCP Compose 演示入口
8. 明确单实例边界并整理测试隔离
```

完成以上内容以后，建议停止继续做：

```text
全 TS 重写
强行所有 Tool MCP 化
为了数量拆多个 Subagent
重新设计已经验证过的 WRITE 安全链
```

项目此后应该从“架构开发阶段”转为：

```text
稳定性收口
→ Benchmark
→ Demo
→ 简历表达
→ 面试理解
```

最终架构的核心表达可以统一为：

> **SmartCS 基于 Pi Harness 构建 Agent 运行时，由 Main Agent 动态选择业务工具；RAG 与订单、退款、工单等能力由 Python 后端提供，WRITE 操作通过确定性授权、请求幂等、ExecutionLedger 与异常结果恢复约束模型副作用，并结合 Pi Session、Context Compaction、MCP、长期记忆和 OpenTelemetry 构建完整的 Agent 工程链路。**
