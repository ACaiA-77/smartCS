# SmartCS × Pi Harness：Claude 复核问题的最终答复与设计修订

> 日期：2026-10-03  
> 用途：回复 Claude Code 对《SmartCS_Pi_Harness_Claude_Execution_Review.md》提出的 P1–P8 问题，并作为下一版迁移计划的强制设计输入。  
> 前提已冻结：**继续迁移 Pi**。目标不是短期 ROI 最优，而是学习成熟 Agent Harness，并把 SmartCS 重构成一个能在面试中讲清 Agent Runtime、Business Runtime、Session Recovery、Tool Safety、Context/Memory 边界的项目。

---

## 0. 最终结论

Claude 提出的 8 个问题大部分成立，但其中 P1、P2、P5 的“修法”需要纠正。

| 编号 | 判断 | 最终决定 |
|---|---|---|
| P1 Pi entry 持久化 | 问题成立，但不能用 `entry_appended` 作为所有 entry 的 WAL | Phase 1 直接使用 Pi **file-backed SessionManager** 作为 transcript Source of Truth；逐 entry 由 Pi 自己 append JSONL。idle 不负责保存。 |
| P2 `ticket_create` | 文档确有自相矛盾 | 修正原则为“**所有 WRITE 必须有可信授权**”，而不是“所有 WRITE 必须两轮确认”。退款两阶段；工单允许同轮明确授权。 |
| P3 Memory 写入 | 成立 | 增加 **Durable Memory Outbox**；不能只靠 `agent_settled` fire-and-forget。 |
| P4 Compliance final 边界 | 成立 | final 定义为“最新一个正常结束、且不含 toolCall 的 assistant `message_end`”；在 `message_end` extension 中同步审核/替换，`agent_settled` 后才发客户端。 |
| P5 可信确认归属 | 问题成立，但不建议把规则表迁 TS | canonical matcher 留 Python，抽成 `WriteAuthorizationService`；TS 只携带认证后的 raw user turn。 |
| P6 History | 成立 | `GET /api/history/{session_id}` 改为 harness-aware projector；Pi session 从 active branch 投影，不再完整双写旧 conversation_event。 |
| P7 `seq` | 成立 | Phase 1 不自建 canonical DB entry 表；若后二期做 mirror，`seq` 由 TS 单 writer 在 lease 下单调分配，`parentId` 表树关系、`seq` 表 append 顺序。 |
| P8 evals | 完全成立 | 旧 Python evals 冻结为 Business Runtime regression；新建 TS/HTTP black-box + fault injection。复用场景与断言，不复用旧 orchestrator 调用方式。 |

额外结论：

1. 原文 §10/§11/§15 合并成一个 **WRITE Unknown Outcome** 状态机。
2. 新增 F13：receipt=processing + lease expired 恢复。
3. 新增 F14：pending_action 过期后迟到确认。
4. Skills 放二期，只承载领域知识/话术，不承载业务 authority。
5. `pi-harness/` 建议与 `python-impl/` 平级，但这是新的架构决定，不是当前 AGENTS.md 已经规定的事实；落地时需更新根 AGENTS.md。
6. 保留非流式 JSON chat 兼容端点；Web 工作台一期可以不改成 token streaming。

---

# 1. P1 — Pi entry 持久化：Claude 抓到了问题，但具体解法不成立

## 1.1 Claude 抓对的地方

原文“idle eviction 时 flush durable entries”确实不够。

必须明确：

> **Agent transcript 不能等 idle 才保存。**

否则 Node 在 turn 中崩溃时，最近 user / assistant / toolResult / compaction 都可能丢失。

## 1.2 但 `entry_appended` 不是“所有 entry 的统一 append 事件”

已核对当前 Pi `coding-agent` 源码与官方 docs。

当前 `AgentSessionEvent` 确实有：

```text
entry_appended
```

但官方定义是：

> extension 通过 `pi.appendEntry()` 追加 custom session entry 时发出。

它不是每次：

```text
SessionManager.appendMessage()
appendModelChange()
appendThinkingLevelChange()
appendCompaction()
...
```

都会统一发出的 WAL event。

普通消息当前的真实顺序是：

```text
agent emits message_end
        ↓
extension message_end
        ↓
public session listeners
        ↓
SessionManager.appendMessage(...)
```

因此 Claude 提议：

```text
subscribe(entry_appended)
→ 每 append 一条写 pi_session_entry
```

会漏正常 user / assistant / toolResult 等 entry，不能作为 canonical persistence。

---

# 2. P1 最终方案：Phase 1 直接使用 Pi 原生 file-backed SessionManager

首版不要：

```text
SessionManager.inMemory()
+
自己从 public events 重造 pi_session_entry
```

改为：

```ts
SessionManager.create(
  SMARTCS_RUNTIME_CWD,
  SMARTCS_PI_SESSION_DIR,
  { id: smartcsSessionId }
)
```

其中 `SMARTCS_PI_SESSION_DIR` 必须是 SmartCS 自己显式配置的服务目录，例如：

```text
/var/lib/smartcs/pi-sessions/
```

并挂载持久卷。

**禁止使用默认 `~/.pi/agent/sessions` 作为服务端生产目录。**

理由：

Pi 自己最清楚完整 SessionEntry 语义，包括：

- message
- parentId / tree
- compaction
- context edit
- model / thinking change
- custom entry
- active branch

首版自己做 DB-backed SessionManager 反而会把“学习 Pi”变成“猜 Pi 内部持久化语义”。

## 2.1 durability point

Pi file-backed SessionManager 在 append entry 时直接追加 JSONL。

所以新的定义是：

```text
Pi transcript durability point
=
对应 SessionManager append 返回之后
```

idle eviction 只负责：

```text
wait until session idle
→ session.dispose()
→ remove from active registry
```

**idle 不再承担保存 transcript 的职责。**

## 2.2 必须承认的剩余 crash window

即使 Pi 自己逐条 append，也无法消灭下面这个窗口：

```text
Python WRITE 已成功
↓
HTTP 响应返回途中
↓
Pi 尚未 append toolResult
↓
Node crash
```

这个问题不能靠 transcript persistence 解决，只能靠：

```text
agent_run_receipt
+
operation_id
+
ExecutionLedger
+
ExecutionReconciler
```

所以必须继续坚持：

> **Agent transcript recovery ≠ side-effect recovery。**

## 2.3 一期 durability 范围要写准确

Pi 当前 JSONL append 可以覆盖：

- Node process crash；
- Node service restart；
- container restart（前提：session dir 是 persistent volume）。

但不要把它宣传成：

> 主机断电级强事务持久化。

它不是数据库事务，当前实现也不是每条都显式 `fsync`。

因此一期验收口径冻结为：

> **process/container crash-safe transcript + ledger-backed business side-effect safety。**

如果后二期要做到 host crash / multi-host 强 durability，再评估正式 DB-backed Session adapter。

## 2.4 多 Node

Phase 1 不必为了学习项目过早实现复杂分布式 transcript store。

推荐首版：

```text
1 个 Pi Harness 实例
+
persistent session volume
+
per-session local mutex / actor queue
```

已经能验证：

- idle 恢复；
- 跨设备续聊；
- process crash；
- container restart；
- request replay。

以后水平扩展再增加：

```text
shared session storage
+
distributed session lease
+
one-session-one-writer
```

---

# 3. P2 — `ticket_create`：修正“WRITE 必须两阶段”这个过度泛化

Claude 指出 §12 与 §37 矛盾是对的。

但是最终修复不应该是：

> 所有 WRITE 都机械改成 prepare → 再问一轮 → confirm。

正确原则应该是：

> **任何用户可见 WRITE 都必须有独立于模型的可信授权，但授权模式可以按业务风险不同。**

定义：

| Tool / Operation | Authorization Mode | 语义 |
|---|---|---|
| refund | `PREPARE_THEN_CONFIRM` | 先评估金额/退款方式并形成 pending action，再要求下一轮确认 |
| ticket_create | `EXPLICIT_SAME_TURN` | 用户本轮已明确表达“我要投诉/帮我创建工单/提交申请”即可 |
| compliance escalation ticket | `SYSTEM_POLICY` | 系统规则触发，用 system principal，不伪装成用户确认 |

## 3.1 为什么 ticket 不强制第二轮确认

现有 `TicketHandlerAgent._has_explicit_create_consent()` 已经是确定性业务规则。

它会把：

```text
投诉流程是什么？
怎么创建工单？
```

和：

```text
我要投诉
帮我创建投诉工单
提交申请
```

区分开。

所以 ticket 当前真正的安全语义是：

> **same-turn explicit authorization**

而不是“模型判断 action=create 就能写”。

这可以保留，而且比强制所有 ticket 都多问一轮更自然。

## 3.2 Tool Schema 仍禁止模型提供 `confirmed`

无论哪种授权模式，都禁止模型工具参数出现：

```json
{"confirmed": true}
```

模型只提供业务意图和参数。

最终 authorization 由 trusted turn + Python policy 计算。

---

# 4. P3 — 用户记忆写入：必须是 Durable Memory Outbox

Claude 说“agent_settled 后 fire-and-forget 推 Python”只解决了“谁触发”的一半。

如果只是：

```text
agent_settled
→ fire-and-forget HTTP
```

Node 恰好在 HTTP 发出前崩溃，这轮用户记忆会永久漏掉。

所以需要 durable outbox。

## 4.1 现有 memory provenance 不能丢

已核对现有 `UserMemoryService.process_message()`。

它不是直接相信调用方传来的文本，而是先：

```text
user_id
session_id
event_id
content
↓
回查 durable USER_MESSAGE source
↓
验证 owner/session/event/content 完全一致
↓
才抽取 memory candidate
```

并且代码明确要求：

> Never pass assistant/tool-generated text here.

这个安全设计应该保留。

## 4.2 新架构保留最小 USER_MESSAGE provenance

Pi transcript 不再完整双写旧 conversation_event。

但为 memory provenance 保留一份最小 durable source ledger，例如：

```text
memory_source_event
-------------------
event_id
session_id
user_id
client_request_id
content
created_at
cleared_at
```

迁移早期也可以继续复用旧 `conversation_event`，但只写 USER_MESSAGE provenance，不再把它当 canonical Agent transcript。

## 4.3 正确触发链路

```text
收到用户请求
 ↓
durably persist raw USER_MESSAGE source
 ↓
agent_run_receipt = processing
 ↓
Pi run
 ↓
compliance 完成
 ↓
agent_settled
 ↓
durably mark:
  receipt = completed
  memory_enqueue_pending = true
 ↓
返回用户
```

后台 dispatcher：

```text
scan completed receipts
where memory_enqueue_pending = true
 ↓
POST /internal/memory/enqueue
 ↓
Python UserMemoryService.process_message(...)
 ↓
Python durable candidate queue
 ↓
mark memory_enqueue_done
```

这样 memory enqueue 即使失败也不会触发 model/tool replay。

## 4.4 WAIT_CONFIRM 也允许写 memory

退款进入待确认并不代表本轮失败。

只要：

- user turn 已 durable；
- run 已 settled；
- 本轮不是异常中止；

就可以正常进入 memory outbox。

---

# 5. P4 — Compliance 的 final answer 边界

Claude 提议“最后一次 tool_execution_end 后到 agent_settled 的 assistant text 全部拼接”仍然不够精确。

一个 run 可能是：

```text
assistant A:
“我先帮你查”
+ toolCall

toolResult

assistant B:
最终回答
```

也可能有 retry、compaction recovery、follow-up。

所以不能按时间区间拼接 text。

## 5.1 定义 Eligible Final Assistant Message

每个 assistant `message_end` 到达时，若同时满足：

```text
role == assistant
stopReason 为正常结束
content 中没有 toolCall block
```

则记为：

```text
eligible_final_candidate
```

若后续又出现新的 candidate：

> **latest wins。**

带 toolCall 的 assistant message 即使含前置 narration，也不是 final answer。

## 5.2 Compliance 放在 `message_end` extension，而不是只放 `agent_settled`

已核对当前 Pi 生命周期顺序：

```text
message_end
↓
extension message_end
↓
public listener
↓
SessionManager.appendMessage
```

并且 `message_end` extension 可以返回 replacement message。

因此最终设计：

```text
raw assistant candidate
 ↓
Compliance rule
 ↓
Compliance LLM review
 ↓
pass:
  原消息
sanitize:
  replacement message
fail:
  safe fallback
 ↓
Pi 持久化审核后的 assistant message
```

这样：

> **用户最终看到的文本 == Pi transcript 保存的文本。**

不会出现“用户收到安全版本，但下一轮模型看到未审核原文”的状态分裂。

## 5.3 SSE 规则

禁止：

```text
message_update.text_delta
→ 直接作为 final answer 发客户端
```

因为此时还没经过最终 Compliance。

实时 SSE 只发确定性的：

```text
status
tool_status
progress
```

例如：

```text
正在查询订单
正在检查退款资格
正在检索知识库
```

这些 status 应由 tool name / runtime state 映射生成。

**不要把模型前置 narration 当 status 原样流出去。**

最终输出：

```text
message_end
→ 得到审核后的 candidate
→ 存 RunOutputBuffer

agent_settled
→ 取 latest eligible candidate
→ SSE final
→ SSE done
```

如果 settled 时没有合法 candidate，返回确定性安全 fallback。

---

# 6. P5 — 可信确认：canonical matcher 不迁 TS，留 Python

Claude 的核心问题成立：

> 可信确认必须明确谁判定。

但我不建议把退款确认正则/规则表复制到 TS。

原因：

> **退款确认是业务授权策略，不是通用 Harness 能力。**

它应该归 Python Business Runtime。

## 6.1 把现有规则从 IntentRouter 抽成 `WriteAuthorizationService`

现有 Python 已经有确定性的退款确认/取消短语识别。

迁移后不要再依赖 IntentRouter Agent，而是抽成纯业务规则：

```text
WriteAuthorizationService
├─ authorize_refund_confirmation(...)
├─ authorize_ticket_creation(...)
└─ authorize_system_escalation(...)
```

## 6.2 信任链

```text
Browser raw user input
 ↓
Node JWT auth
 ↓
TrustedTurnEnvelope
  account_id
  business_user_id
  session_id
  client_request_id
  raw_user_message
  raw_message_hash
 ↓
signed internal service request
 ↓
Python WriteAuthorizationService
  load pending_action
  verify ownership
  verify session
  deterministic phrase matcher
  verify action not expired
 ↓
authorization decision
 ↓
ToolExecutor
```

这里真正传给：

```text
ToolExecutionContext(confirmed=True)
```

的 `True` 必须由 Python 自己计算。

模型不能提供。

## 6.3 TS 的职责

TS 可以：

- 看见 pending_action 摘要；
- 决定是否把 `refund_confirm` 工具展示给 Main Agent；
- 给 UI 展示“待确认”；
- 在 prompt 中说明下一步。

但即使 TS 或模型错误调用：

```text
refund_confirm
```

Python 仍必须 fail closed。

---

# 7. P6 — History endpoint 必须 harness-aware

已核对当前：

```text
GET /api/history/{session_id}
```

checkpoint 开启时读取：

```text
checkpoint_store.history(...)
```

Pi session 不再完整双写旧 `conversation_event` 后，该接口必须改造。

## 7.1 前端 DTO 保持稳定

建议继续返回：

```json
{
  "session_id": "...",
  "messages": [
    {
      "role": "user",
      "content": "...",
      "created_at": "..."
    },
    {
      "role": "assistant",
      "content": "...",
      "created_at": "..."
    }
  ]
}
```

普通客服历史默认不暴露：

- system
- toolResult
- thinking
- custom internal state

调试 trace 另开 internal/admin endpoint。

## 7.2 分发规则

```text
conversation_session.harness_version

legacy
→ legacy history projector

pi
→ Pi history projector
```

Pi projector：

1. 打开 session file；
2. 读取 raw session tree 的 **active branch**；
3. 处理 context edit 对用户/assistant 可见内容的 replacement / omission；
4. 只投影 user / assistant；
5. 返回稳定 DTO。

特别注意：

> **不要用 compaction 后的 model-context projection 作为 UI history。**

Compaction 是“给模型少看一点”，不等于“用户过去的聊天记录被删除”。

## 7.3 DELETE endpoint 也要改

Pi harness 下清历史需要：

```text
acquire session lease
→ refuse if active write/run
→ dispose active AgentSession
→ delete Pi session storage
→ clear pending business state
→ apply memory provenance clear policy
→ delete platform session / reset according to现有 API 语义
```

不能只清旧 SessionStore。

---

# 8. P7 — `seq` 的来源

如果 Phase 1 采用本文方案：

> canonical transcript 直接是 Pi JSONL，不需要再造 `pi_session_entry.seq`。

JSONL 物理 append 顺序就是 chronology；

```text
entry.parentId
```

表示 tree topology。

## 8.1 如果二期做 DB mirror / DB-backed adapter

则：

```text
pi_session_entry
----------------
session_id
seq
entry_id
entry_json
entry_hash
```

中的 `seq` 定义为：

> **TS single writer 在 session lease 下分配的单调 append 序号。**

必须满足：

- 每 session 独立；
- 从 1 单调递增；
- 不从 timestamp 推导；
- 不从 entry id 推导；
- replay / restore 按 `seq` 读取。

区别：

```text
parentId = 树关系
seq      = 写入先后
```

二者不能混为一谈。

如果只是 analytics / backup mirror，可在：

```text
turn_end
agent_settled
idle eviction
```

增量 mirror，但 mirror 不作为 Phase 1 authority。

---

# 9. P8 — evals 必须拆成“旧资产回归”和“新 Harness 黑盒”

Claude 这一条完全正确。

当前 `evals/scenarios.py` 直接构造：

```text
ChatOrchestrator
SessionStore
MCPToolServer
ToolExecutor
DeterministicEvalLLM
```

并调用：

```python
runtime.orchestrator.ainvoke(...)
```

它只能证明 legacy Python orchestrator。

## 9.1 旧 Python evals 不删除

重新定义为：

> **Business Runtime Regression Suite**

继续测试：

- ToolExecutor READ retry；
- WRITE 单次尝试；
- ExecutionLedger；
- ExecutionReconciler；
- domain DB invariant；
- RAG retriever；
- memory queue；
- ticket/refund authorization helpers。

这些都是迁 Pi 后仍保留的 Python 资产。

## 9.2 新建 Pi Harness Test Suite

建议：

```text
pi-harness/tests/
├─ unit/
├─ integration/
└─ e2e/
```

### TS Unit

测试：

- SessionRegistry；
- per-session queue；
- request receipt state machine；
- output buffer；
- history projector；
- service auth envelope；
- tool contract；
- status mapping；
- compliance candidate selection。

### Pi Runtime Integration

使用 Pi fake/faux provider：

- model → tool call；
- tool result → final answer；
- multiple tool turns；
- compaction；
- abort；
- agent_settled；
- session resume。

### Cross-service E2E

真正走：

```text
HTTP / SSE
→ Pi Node
→ internal HTTP
→ Python test runtime
→ SQLite/MySQL test DB
```

验证：

- ownership；
- request dedupe；
- side effect；
- recovery；
- history；
- memory outbox；
- final compliance。

## 9.3 复用“业务 scenario”，不要复用旧调用实现

例如旧：

```text
case_05_refund_confirmation
```

在新 Harness 下重写成：

```text
POST chat: 帮我退款 ORD...
assert refund_count == 0

POST chat: 确认退款
assert refund_count == 1

replay same client_request_id
assert refund_count == 1
```

继续复用业务断言；

不再复用：

```text
ChatOrchestrator.ainvoke
ObservedExecutor
DeterministicEvalLLM
```

作为 E2E 入口。

## 9.4 工作量重新入账

测试不应写成迁移完成后的附属工作。

正式计划单列：

```text
TS unit/integration
Cross-service black-box
Fault injection harness
Legacy scenario port
```

因此原 3–5 人周估计不再作为承诺。

# 10. 合并原 §10 / §11 / §15：统一成 WRITE Unknown Outcome 状态机

这三个章节本质上都在解决同一个问题：

> **调用方不知道 WRITE 到底有没有发生。**

统一定义 operation 状态：

```text
PREPARED
SENT
COMPLETED
FAILED
UNKNOWN
```

真正权威仍是：

```text
ExecutionLedger / Domain DB
```

而不是 Node 的 HTTP 返回值。

## 10.1 operation_id 必须在 WRITE 前 durable

正确顺序：

```text
LLM requests write tool
 ↓
derive / allocate operation_id
 ↓
durably attach operation_id to agent_run_receipt
 ↓
send internal request to Python
 ↓
ToolExecutor / Ledger
```

禁止：

```text
先执行 WRITE
↓
再记 operation_id
```

否则 Node crash 后没有 stable key 可 reconcile。

## 10.2 恢复决策表

| Node 观察 | Ledger / Domain | 决策 |
|---|---|---|
| 尚未发送 WRITE | 无记录 | 可以安全重新发起 |
| HTTP 成功 | COMPLETED | 恢复 tool result / final answer |
| 明确业务拒绝 | 已有确定业务结果 | 不重复副作用，返回业务结果 |
| HTTP timeout / socket disconnect | 未知 | 标记 UNKNOWN，禁止 blind retry，先 reconcile |
| Node crash，receipt=processing | COMPLETED | 从 ledger 恢复 authoritative result |
| Node crash，receipt=processing | FAILED | 恢复失败结果 |
| Node crash，receipt=processing | 可证明未执行 | 才允许重新发起 |
| 无法确定 | UNKNOWN | 继续 reconcile，不能创建第二个 operation |

## 10.3 Pi transcript 缺 toolResult 时如何恢复

如果恢复时发现：

```text
Ledger = COMPLETED
但 Pi transcript 没有 toolResult
```

不要假装原 tool execution 没发生，更不能重放真实 WRITE。

恢复层应使用 authoritative operation result：

- 优先恢复成一个显式的 recovery tool/result entry 或 continuation context；
- 如果目标 Pi 版本没有安全的 transcript repair API，则直接由 Harness 生成确定性“业务已完成”结果并结束该 request；
- 后续新 turn 仍从 Python business state / ledger 重新注入事实。

**Phase 5 必须用目标 Pi 版本做 spike，选定一种受支持的恢复方式后再写死实现。**

---

# 11. 新增 F13 — receipt=processing 且 lease 已过期

场景：

```text
Node A acquire session lease
 ↓
receipt = processing
 ↓
Node A crash
 ↓
lease TTL expires
 ↓
Node B 收到同 client_request_id
```

Node B 正确流程：

```text
acquire lease
 ↓
load receipt
 ↓
receipt == processing
 ↓
inspect recorded operation_id(s)
 ↓
if WRITE exists:
    reconcile ledger
else:
    inspect Pi transcript / run boundary
 ↓
resume only the provably safe portion
```

禁止：

```text
lease 过期
→ 把整个 prompt 从头跑一遍
```

验收必须证明：

- 不重复真实 WRITE；
- 不生成第二个 operation_id；
- completed receipt 最终只有一个 canonical response。

---

# 12. 新增 F14 — pending_action 过期后的迟到确认

pending action 必须有明确生命周期：

```text
pending_action_id
session_id
user_id
type
payload
status = pending | consumed | cancelled | expired
created_at
expires_at
operation_id
```

用户过期后才说：

```text
确认退款
```

Python `WriteAuthorizationService`：

```text
load pending
 ↓
verify owner/session
 ↓
now > expires_at
 ↓
atomically mark expired
 ↓
return PENDING_ACTION_EXPIRED
```

结果：

- 不调用 `refund_create`；
- 不复用旧 amount / refund_mode；
- 不消费旧 operation；
- 回复“确认已过期，请重新发起退款评估”。

---

# 13. Skills：二期可用，但不能成为业务 authority

Pi Skills 很适合：

- 退款政策说明；
- 工单标准话术；
- 客服沟通风格；
- 产品支持 SOP；
- knowledge_search 使用建议；
- tool 选择说明。

它可以体现 progressive disclosure，也有学习价值。

但 Skills 不能决定：

- 当前订单是否可退款；
- pending_action 是否仍有效；
- 用户是否已确认；
- WRITE 是否已经执行；
- WRITE 是否允许重试。

这些仍由 Python Business Runtime 决定。

---

# 14. `pi-harness/` 目录位置

建议最终布局：

```text
D:\Workspace_for_Codex\project005_SmartCS\
├─ AGENTS.md
├─ python-impl\
└─ pi-harness\
```

即 `pi-harness/` 与 `python-impl/` 平级。

但要纠正一句：

> “符合 AGENTS.md 的仓库布局约定”

当前根 `AGENTS.md` 并没有预先规定 Node service 必须平级；它只说明当前 active Python service 在 `python-impl/`，planning notes 放 `docs/`。

所以平级是这次重构的**新架构决定**。

正式创建 `pi-harness/` 时必须同步更新根 `AGENTS.md`，加入：

- Node service 职责；
- install / test / run 命令；
- lint/style；
- 与 Python service 的接口边界；
- docs 放置规则。

---

# 15. Web 工作台兼容：保留非流式 JSON endpoint

这个建议直接采纳。

一期 final answer 本来就是：

```text
buffer
→ compliance
→ agent_settled
→ send final
```

所以保留：

```text
POST /api/chat
→ JSON final response
```

几乎没有额外架构成本。

同时新增：

```text
POST /api/chat/stream
→ SSE status events
→ final
→ done
```

现有 Web Workbench 一期继续用 JSON，不需要为了 Pi 迁移同步重写前端。

---

# 16. Source of Truth 再冻结一次

| 状态 | Authority |
|---|---|
| session ownership / title / harness_version | MySQL Platform Session |
| Agent transcript / compaction / active branch | Pi native Session file |
| raw user message provenance | minimal USER_MESSAGE source ledger |
| request 是否已处理 | Agent Run Receipt |
| current business pending state | Python / MySQL Business State |
| trusted write authorization | Python WriteAuthorizationService |
| WRITE side effect | ToolExecutor + ExecutionLedger + Domain DB |
| long-term user memory | Python Memory Store |
| Node AgentSession object | runtime cache，不是 authority |

这张表必须进入下一版主计划。

---

# 17. 修订后的 Phase 顺序

## Phase 0 — Pi Runtime Spike

只验证：

- exact Pi version；
- custom provider；
- fixed server resource loader；
- 禁用 coding built-in tools；
- file-backed SessionManager；
- custom READ tool；
- event lifecycle；
- `message_end` replacement；
- `agent_settled`；
- abort；
- JSON final + SSE status。

**不接真实 WRITE。**

## Phase 1 — Session / Receipt Foundation

完成：

- `harness_version`；
- Pi explicit session dir；
- SessionRegistry；
- per-session mutex；
- agent_run_receipt；
- minimal USER_MESSAGE provenance；
- idle dispose / reload；
- request replay；
- history projector；
- clear/delete semantics。

Phase 1 故障测试：

- process restart；
- container restart；
- duplicate request_id；
- same request_id + different payload；
- two concurrent requests same session；
- idle reload。

## Phase 2 — Read-only Business Tools

接：

```text
knowledge_search
order_query
ticket_query
refund_evaluate
risk_check
```

要求：

- internal service JWT；
- ownership；
- model 不能控制 user_id；
- Python 仍是 tool policy owner；
- RAG 不拆 service。

## Phase 3 — Context / Memory / Compliance

完成：

- Python Business Context Snapshot；
- memory prefetch；
- Durable Memory Outbox；
- Pi compaction；
- eligible final candidate；
- `message_end` compliance replacement；
- JSON final / SSE status。

## Phase 4 — WRITE Shadow

Pi 只产生 write tool plan，不实际执行。

比较 legacy：

- 是否需要退款；
- 是否需要创建工单；
- 参数是否正确；
- 是否需要等待确认；
- authorization mode 是否正确。

## Phase 5 — WRITE Enable + Recovery

接：

```text
refund_confirm
ticket_create
system escalation
```

必须实现：

- WriteAuthorizationService；
- operation_id-before-send；
- UNKNOWN state；
- reconcile；
- F1–F14。

## Phase 6 — Observability

统一：

```text
trace_id
session_id
client_request_id
agent_run_id
tool_call_id
operation_id
```

## Phase 7 — Cohort Rollout

按：

```text
conversation_session.harness_version
```

固定整个 session。

禁止按 intent 在 legacy / pi 间切。

## Phase 8 — Optional MCP / Skills

HTTP 稳定后再尝试：

- READ tool MCP；
- Skills progressive disclosure；
- 评估 Subagent，但不作为成功条件。

---

# 18. Claude 下一版计划必须明确回答的 12 个问题

正式编码前，请在主计划里逐项给出确定答案：

1. Pi exact version 是什么？
2. transcript canonical storage 是什么？
3. Node process/container crash 后如何 reopen 同一 Pi session？
4. 同一 session 的并发请求如何串行？
5. `client_request_id` 如何实现 request-level exactly-once experience？
6. `operation_id` 在什么时候 durable？
7. HTTP timeout / disconnect 后如何区分 FAILED 与 UNKNOWN？
8. 谁有权把 `confirmed=True` 传给 ToolExecutor？
9. ticket same-turn authorization 与 refund two-turn confirmation 的策略差异是什么？
10. memory source provenance 如何保留且如何 durable enqueue？
11. history API 如何在 legacy / pi session 间分发？
12. final assistant message 的精确定义、审核点和对外发送点是什么？

以上 12 个问题有任一仍用“后续实现时再决定”回答，则 Phase 1 之前仍不应进入全量开发。

---

# 19. 修订后的 F1–F14 故障矩阵

| Case | 故障 | 必须保证 |
|---|---|---|
| F1 | Node 在 LLM 前崩溃 | 无 side effect，可恢复 request |
| F2 | READ tool HTTP 中断 | 可安全重试 |
| F3 | WRITE 发送前 Node 崩溃 | Ledger 无 operation，可重新发起 |
| F4 | Python WRITE 成功、HTTP 响应丢失 | UNKNOWN → reconcile，禁止 blind retry |
| F5 | WRITE 成功但 Pi toolResult 未 append | 从 ledger 恢复，不能再执行 WRITE |
| F6 | final 生成后 receipt completed 前崩溃 | 不重复 WRITE，可重新生成/replay answer |
| F7 | 同 request_id 重发 | 不重复 Agent turn / side effect |
| F8 | 同 session 两设备并发 | 单 writer、确定顺序 |
| F9 | SSE 断开且 WRITE 正在执行 | abort 不等于业务失败，走 reconcile |
| F10 | compaction 后确认退款 | pending state 从 Python 恢复 |
| F11 | tool result 含 prompt injection | 仅作为不可信数据 |
| F12 | Node A crash、Node B/重启后恢复 | 同 session / transcript / business facts |
| F13 | receipt=processing + lease expired | 新 owner 先恢复，不从头盲跑 |
| F14 | pending_action expired 后迟到确认 | 拒绝旧确认，不产生 WRITE |

---

# 20. 对 Claude 这轮复核的最终评价

这轮复核有价值，尤其正确抓到了：

- transcript durability 没写死；
- ticket WRITE 原文存在表述矛盾；
- memory write path 缺失；
- final compliance 边界模糊；
- history 兼容遗漏；
- eval 重写工作量漏算。

但正式执行时必须采用本文的三处修正，而不是直接照 Claude 的建议实现：

### 修正 1：不要把 `entry_appended` 当所有 Pi entry 的持久化事件

第一阶段直接使用 Pi native file-backed SessionManager。

### 修正 2：不要把“所有 WRITE 必须两阶段”当成统一规则

统一的是：

> **所有 WRITE 必须有模型之外的 trusted authorization。**

退款是 two-turn；工单可以 same-turn explicit consent。

### 修正 3：不要把 canonical confirmation matcher 搬到 TS

退款/工单授权是业务规则，Python Business Runtime 才是最终 authority。

TS Harness 负责：

```text
Agent decides what it wants to do.
```

Python Runtime 负责：

```text
The business decides whether it is allowed to happen.
```

这条边界应作为整个 SmartCS × Pi 重构的核心设计原则。

---

# 21. 给 Claude Code 的下一步执行指令

1. 基于本文修改 `docs/pi-replatform-plan.md`，不要开始全量编码。
2. 把 P1 的 session persistence 改为 **Pi native file-backed SessionManager + explicit persistent session dir**。
3. 把 WRITE 安全抽象改为 `WriteAuthorizationMode`，不要统一强制两轮。
4. 设计 `WriteAuthorizationService`，canonical matcher 留 Python。
5. 增加 Durable Memory Outbox 和 minimal USER_MESSAGE provenance。
6. 明确 `message_end → compliance replacement → agent_settled → client final` 生命周期。
7. 补 history/clear harness-aware projector。
8. 把旧 evals 定位成 Business Runtime regression，新建 Pi Harness black-box/fault suite。
9. 把 F13、F14 纳入验收矩阵。
10. 输出修订后的详细计划后再进入 Phase 0；未经上述设计闭环，不直接实现 WRITE migration。
