# SmartCS 架构说明

这份文档面向代码评审、项目演示和面试沟通，描述**当前**实现。它不把可选的生产扩展
写成已经存在的能力。

相关文档：

- 边界规则（模型能做什么、Python 允许什么）→ [runtime-boundaries.md](runtime-boundaries.md)
- 崩溃与恢复语义 → [recovery.md](recovery.md)
- Harness 自身的结构 → [`../../pi-harness/README.md`](../../pi-harness/README.md)
- 历史迁移过程（各阶段报告）→ `../../pi-harness/PHASEn_REPORT.md`

## 1. 双层架构

```text
                    ┌──────────────────────────────────────────┐
   浏览器 / 客户端 → │  pi-harness  (Node 22 / TypeScript / Pi) │
                    │  Agent Loop · LLM · Session · Compaction │
                    │  Tool Selection · Skills · MCP Client    │
                    │  Streaming · Lifecycle · Observability   │
                    └───────────────────┬──────────────────────┘
                                        │  内部 HTTP
                                        │  每轮现签 Service JWT（身份信封）
                    ┌───────────────────▼──────────────────────┐
                    │  python-impl (FastAPI)  Business Runtime │
                    │  RAG · ToolExecutor · Write Authorization│
                    │  ExecutionLedger · PendingAction         │
                    │  Order / Refund / Ticket · Memory        │
                    │  Compliance · Business State             │
                    └──────────────────────────────────────────┘
```

分界的判据只有一条：**权威业务状态和副作用安全不跟着模型运行时漂移。**
模型运行时（Pi）可以被替换、升级、换供应商；订单、退款、工单、账本和长期记忆
必须留在一个可以被确定性审计的地方。

## 2. 一次客户请求的完整路径

```text
POST /api/chat   （或 /api/chat/stream）
  1 本地验签用户 JWT，取出 account_id 候选
  2 POST /internal/auth/verify          权威身份：account → business_user
  3 SessionRegistry.acquire(session)    同 session 串行（进程内 mutex）
  4 agent_run_receipt 判定              replay / 409 / run
  5 INSERT memory_source_event          在任何 LLM 活动之前落 provenance
  6 预取 /internal/context/turn-snapshot 权威业务事实（在模型运行之前）
  7 先查后建 Pi session → prompt
  8 工具调用：READ 走 /internal/tools/execute；WRITE 走授权 → 落库 → 执行
  9 合规复核 /internal/compliance/review
 10 receipt → completed
 11 响应
```

关键点：

- **步骤 5 早于步骤 7**。崩溃永远不会留下一个"模型跑过了但来源消息没记账"的状态。
- **步骤 6 早于步骤 7**。业务事实在模型看到问题之前就已确定，模型无法通过措辞
  影响它读到的事实。
- 步骤 6 需要步骤 2 的结果（Service JWT 要带 `business_user_id`），因此两者不能并行；
  但它仍然在 `pi.on` 路径之外，符合"hook 不许做业务 IO"的红线。

## 3. 四类状态及其归属

| 状态 | 存放 | 谁写 | 权威性 |
|---|---|---|---|
| 业务状态（订单/退款/工单） | SQLite | Python 业务域 | 权威 |
| 执行幂等与结果 | `ExecutionLedger` | Python `ToolExecutor` | 权威 |
| 请求回执与 provenance | MySQL（`agent_run_receipt` / `memory_source_event`） | Harness 写，Python 读并复核 | durable 事实 |
| 对话 transcript | Pi session 文件（独立 volume） | Harness | 会话记录，**不是**业务事实 |
| 长期记忆 | MySQL（candidate / profile / episode） | Python `UserMemoryService` | 权威 |

"transcript 不是业务事实"是这套划分里最容易违反、也最贵的一条：一旦有代码
从模型说过的话里反推订单状态，前面所有确定性保证都会作废。

## 4. 读工具链

```text
order_query / ticket_query / refund_evaluate / risk_check
        ↓ /internal/tools/execute
   剥离模型传入的身份字段 → 从 claims 重绑 user_id
        ↓
   ToolExecutor（timeout / READ retry / idempotency）
        ↓
   业务域 → SQLite
```

`knowledge_search` 走另一条路（共享语料、无用户身份）：

```text
        ↓ Pi MCP Client（streamable HTTP，服务级 token）
   Python MCP Gateway
        ↓
   HybridRetriever = Dense(FAISS bge-m3) + Sparse(BM25 全局语料) → RRF → Cross-Encoder
```

为什么只有它走 MCP，见 [runtime-boundaries.md](runtime-boundaries.md) §5。

### RAG 延迟分段计时

`internal_api/rag_timing.py` 提供**不修改 `rag/`** 的分段计时：它在运行时包装
retriever 实例的协作对象（dense / sparse / reranker）与 `rag.retriever` 命名空间里的
RRF、排序辅助函数，记录 dense / BM25 / rank_merge / RRF / rerank / serialize /
retrieve_total 的 P50/P95。所有包装都是直通的：记录耗时，原样返回结果。

```bash
python -m internal_api.rag_timing                 # 用 RAG benchmark 的真实查询压测
python -m internal_api.rag_timing --queries 10 --json out.json
```

服务内默认关闭，`SMARTCS_RAG_TIMING=1` 打开（MCP 网关会安装到它的 retriever 上）。

## 5. 写操作链

```text
模型提出 refund_confirm(pending_action_id) / ticket_create(title, description, ...)
        ↓
WriteAuthorizationService 读**持久化的用户消息**判定
        ↓
operation_id / pending_action 落库（先于副作用）
        ↓
ToolExecutor 执行（confirmed=True 由服务端构造）
        ↓
ExecutionLedger 记录权威结果
```

- 模型只提供业务参数；身份、授权、幂等键、payload hash 全部由服务端注入。
- 两段式退款：`refund_evaluate` 生成待确认动作 → 用户明确确认 → `refund_confirm`。
- 传输失败时从账簿裁决，绝不盲重试（见 [recovery.md](recovery.md) §2）。

## 6. 合规

两层，且权威在 Python：

1. **规则掩码**（始终执行）：确定性正则，如 PII 脱敏、收益承诺拦截；
2. **LLM 复核**（可选）：同一套规则命中后的语义复核。

`fail` 判定返回确定性兜底话术，并且**替换 transcript 中的消息**，使用户看到的、
记录下来的和返回的完全一致。规则判定不通过时不会因为模型"补充说明"而放行。

## 7. Legacy 路径

`ChatOrchestrator` / `IntentRouter` / `KnowledgeRAGAgent` / `TicketHandler` /
`RefundHandler` 仍在仓库中，服务 `harness_version='legacy'` 的会话与不带 Pi 会话的
通用入口。它们**和主链路共享同一套业务能力**（同一个 `ToolExecutor`、同一批领域
服务、同一个账本），因此不是第二套业务实现——只是另一条调用它的编排路径。

新能力（Pi 会话、上下文压缩、MCP、长期记忆 outbox、技能）只挂在主链路上。
判断一段代码属于哪条路径，看它是否经过 `/internal/*` 通道。

## 8. 本地目标与不做的部分

这是面向工程实践的本地 Sandbox / 演示项目，不代表真实生产部署。**明确不做**：

- Python → TypeScript 全量重写（收益低，会重新引入已解决的幂等与恢复问题）；
- 把所有工具强行 MCP 化（会改变身份与授权模型）；
- Harness 多副本与分布式 session lease（当前按单实例声明，见 runtime-boundaries §6）；
- 为了数量拆多个 Subagent。
