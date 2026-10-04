# 运行时边界（Runtime Boundaries）

这份文档回答一个问题：**当模型判断错误时，为什么系统不会做错事。**

## 0. 一句话规则

> Agent 决定**想做什么**；Python 决定**是否允许发生**。

模型运行时可替换，权威业务状态与副作用安全不跟着 Harness 漂移。

## 1. 职责划分

```text
pi-harness (TypeScript / Pi)        python-impl (Python / FastAPI)
──────────────────────────────      ──────────────────────────────────────
Agent Loop / LLM / Session          RAG / ToolExecutor
Context Compaction / Tool Selection Write Authorization / ExecutionLedger
Skills / MCP Client / Streaming     PendingAction / Order / Refund / Ticket
Agent Lifecycle / Observability     Long-term Memory / Compliance / Business DB
```

Harness **不做**的事（由设计保证，不是约定）：

1. 不持久化业务状态；
2. 不自铸可信身份；
3. 不把模型 transcript 当作业务事实；
4. 不为写操作编造结果。

## 2. 身份：只有 Python 有权说"你是谁"

```text
浏览器 / API 客户端
   ↓ 用户 JWT（公开 token，iss=smartcs）
pi-harness：本地验签，取出 account_id 候选值
   ↓ POST /internal/auth/verify
   Authorization: Bearer <Service JWT>（短 TTL，iss=smartcs-pi-harness）
   body: { user_jwt, session_id }
python-impl：以 user JWT 为唯一身份来源解析 account → business_user
   ↓ 返回权威身份
```

要点：

- Harness 提交的是**原始用户 token**，不是它自己的身份断言；`account → business_user`
  的映射只有 Python 一处权威。
- 后续每一次内部调用都带一枚**当轮现签**的 Service JWT，claim 含
  `account_id` / `business_user_id` / `session_id` / `client_request_id`。
  Token 由 Harness 用共享密钥签出，但**内容**来自 Python 上一次的权威答复。
- 服务间通道（`/internal/*`）与公开 API 使用**不同的密钥与 audience**，且只应
  在内网可达。

## 3. 工具参数：业务参数属于模型，其余属于运行时

模型可见 schema 只包含它真正负责决定的业务参数。以下字段**不进模型 schema**：

```text
user_id / business_user_id / account_id / session_id
client_request_id / request_payload_hash / confirmed / approval_id
```

即使它们出现在请求里（例如被入侵或有 bug 的 Harness 发来），Python 也会：

1. **剥离**这些字段，并记 warning 审计（`audit.strippedFields`）；
2. 从**已验证的 claims** 重新绑定 `user_id`（`audit.forcedFields`）；
3. 写操作另由服务端生成 `client_request_id` 与 canonical payload hash。

因此"模型填了一个别人的 user_id"在效果上等于什么都没填。

## 4. 写操作：授权在前，执行在后

```text
模型提出 refund_confirm / ticket_create
   ↓  （只是提议）
Python 授权层读取**持久化的用户消息**并判定
   ↓  落库：operation_id / pending_action（先于任何副作用）
   ↓  只有此时才调用 ToolExecutor，confirmed=True 由服务端构造
业务域写入
   ↓
ExecutionLedger 记录权威结果
```

- `refund_confirm` 不接受模型的业务参数：金额、订单、退款方式都来自**冻结的
  pending_action 快照**，模型只提供 `pending_action_id`。
- 两段式退款：`refund_evaluate` 形成待确认动作 → 用户明确确认 → `refund_confirm`。
  Prompt 明确要求模型不得自行判断"用户已确认"。
- 幂等键是 `client_request_id` + canonical payload hash：跨用户或 payload 冲突
  不会返回旧记录。

## 5. 传输边界：为什么不是"全 MCP"

| 工具 | 传输 | 原因 |
|---|---|---|
| `knowledge_search` | MCP（streamable HTTP） | 共享语料，无 per-user 身份；渠道只需服务级 token |
| `order_query` / `ticket_query` / `refund_evaluate` / `risk_check` | 内部 HTTP | 依赖 per-turn 身份 |
| `refund_confirm` / `ticket_create` | 内部 HTTP | 依赖 per-turn 身份 **+ 授权 + 幂等 + reconcile** |

MCP 连接是**长连接、按 server 认证**的，天然没有 per-turn 用户身份。把身份绑定的
工具搬过去，等于把用户身份走私过渠道——那是安全模型变更，不是传输方式变更。
所以这是**最终边界**，不是迁移中的中间态。

## 6. 部署边界：单实例

同一 session 的 single-writer 保证来自 `SessionRegistry` 的**进程内** mutex
（`pi-harness/src/session/registry.ts`）。这对一个 Harness 进程是完备的。

> **当前 Harness 按单实例运行，不声明支持横向多副本。**

两个副本之间没有共享 lease，同一 session 仍可能被双写。若将来需要多实例，
升级路径是：共享 lease（MySQL / Redis）+ session 路由。当前不预先实现。

Python 侧无此限制：`agent_run_receipt` 的状态迁移是条件更新（CAS），
`memory_enqueue_status` 的推进同样是 CAS，所以即使出现重复投递也不会重复生效。

## 7. 长期记忆：恢复不依赖任何进程内状态

```text
请求完成 → agent_run_receipt.memory_enqueue_status = pending
   ↓  MemoryOutboxDispatcher 扫描（durable）
   ↓  逐行重建身份：conversation_session.account_id
   │                + memory_source_event.business_user_id / event_id
   ↓  现签 Service JWT
   ↓  POST /internal/memory/enqueue
Python 再次校验 provenance 后交由 UserMemoryService
   ↓  pending → done
```

核心原则：**Memory Outbox 的恢复不得依赖活着的 AgentSession、TurnContext
或任何 Node 进程内状态。** 详见 [recovery.md](recovery.md)。

## 8. 观测：审计可以丢尾巴，记忆不能丢

| 通道 | 语义 | 理由 |
|---|---|---|
| Audit（`audit_event`） | best-effort，溢出丢弃并计数 | 审计可以丢尾部，但一旦入库不得失真或重复（`event_id` 幂等） |
| Memory（`memory_source_event` → candidate） | durable，崩溃后可补投 | 记忆丢失不可接受 |
| Trace（OTel） | 不阻塞任何 hook | 观测失败不得影响业务 |

三者语义不同是刻意的：把审计做成 durable 会让它反压请求路径，把记忆做成
best-effort 会真的丢用户数据。
