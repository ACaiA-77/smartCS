# 崩溃与恢复（Recovery）

系统的恢复语义只有一条总原则：

> **恢复只依据持久状态。任何"进程还活着时记得的东西"都不算数。**

这条原则在四个地方落地：请求回执、写操作、长期记忆 outbox、Pi transcript。

## 1. 请求幂等：`agent_run_receipt`

每个请求以 `(session_id, client_request_id)` 为唯一键，落一行收据：

```text
new request → processing → completed
                         ↘ failed_recoverable
```

| 请求到来时看到的行 | 判定 | 动作 |
|---|---|---|
| 不存在 | 新请求 | 插入并执行 |
| `completed` | 已答过 | **replay**：直接返回存下的响应，不跑模型 |
| `failed_recoverable` | 可重跑 | 条件更新抢占（CAS），谁抢到谁跑 |
| `processing` | 崩溃残留 | 标记为 orphan 并回收重跑 |

`request_hash` 是消息文本的 sha256：同一个 `client_request_id` 配不同 payload
一律 409，绝不猜测用户想重放哪一个。

**单写者前提**：回收 orphan 之所以安全，是因为调用方持有该 session 的租约
（`SessionRegistry`）。没有租约，一个正在跑的请求和一个崩溃残留长得一模一样，
会被双跑。因此这些方法不得在租约之外调用——见 [runtime-boundaries.md](runtime-boundaries.md) §6。

## 2. 写操作：先落库，再发出去

写操作的危险窗口是**传输失败**：HTTP 断了，写可能发生了，也可能没有。
Harness 不猜。

```text
1. 授权（读持久化的用户消息，服务端判定）
2. operation_id / pending_action 落库        ← 先于任何副作用
3. 才调用 ToolExecutor 执行
4. 结果写入 ExecutionLedger（权威）
```

如果第 3 步的响应丢了，Harness 从收据里的 `open_write_operations`（第 2 步写的）
找到 `operation_id`，再问 `POST /internal/operation_status`：

| 账簿裁决 | 处理 |
|---|---|
| `COMPLETED` | 副作用确实发生了 → 用确定性话术结束请求，**不重放** |
| `FAILED` | 确定性失败，未产生业务变更 |
| `PROVABLY_NOT_EXECUTED` | 未执行；可以重试，但重试策略不属于传输层，故交给人工/编排 |
| `UNKNOWN` | 结论不明 → **停止自动处理**，要求人工核对。绝不盲重试 |

"绝不盲重试"是这里唯一真正重要的规则：一次盲重试足以产生第二笔退款。

## 3. 长期记忆 outbox

### 为什么需要它

用户消息在**模型运行之前**就写进 `memory_source_event`。之后：

```text
请求完成 → agent_run_receipt.memory_enqueue_status = pending
        ↓  MemoryOutboxDispatcher（后台，按 batch 扫描）
        ↓  POST /internal/memory/enqueue
        ↓  Python 复核 provenance
   pending → done
```

崩溃在任何位置都只是让这行保持 `pending`，下一个进程自然补投。

投递语义是 **At-least-once delivery + idempotent consumer**：崩溃意味着这一行会被
**再投一次**，而不是"网络层只投一次"。重复投递不会产生重复的记忆结果——落库那一步
以 `memory_enqueue_status='pending'` 为条件的 CAS 只会成功一次，消费端（
`/internal/memory/enqueue` 的候选写入）本身也是幂等的。最终业务效果是 effectively-once，
但**不要把它描述成网络投递的 exactly-once**。

### 恢复不能依赖进程内状态

早期实现从内存中的 `TurnContext` 取身份。请求一结束它就被清空，idle 驱逐、
进程重启、`kill -9` 之后更不可能存在。现在的实现**逐行从持久状态重建**：

```text
receipt.session_id            → conversation_session.account_id
receipt.(session, request)    → memory_source_event.business_user_id + event_id
```

再用这些值**现签**一枚 Service JWT。全程不读任何 Node 进程内状态。

`memory_source_event` 已被清除（`cleared_at` 非空）时，该行不构成投递依据，
这是"用户要求遗忘"的正确反映。

### 失败语义

- 投递失败 → 计数 +1，下一轮重试；超过阈值（默认 5）后置为 `failed` 留档，
  避免毒消息无限自旋。
- 失败**只重试记忆**：这条路径没有任何 session 句柄，也不调用模型或工具，
  因此不可能重放一个 LLM turn 或一次工具调用。
- 重复投递不会产生重复 candidate：`user_memory_candidate` 的写入以
  `(source_event_id, candidate_key)` 幂等，重复只增加 `existing_count`。
  收据侧的 `pending → done` 也是 CAS，重复扫描不会重复计数。

### 关闭时的语义

`SIGTERM` 时会尝试一次有界的最终投递（默认上限 15s）。投递不完也没关系：
行仍是 `pending`，下次启动继续。记忆投递是 durable work，但**不允许**把关停
挂死在不可达的运行时上。

对照：审计（`audit_event`）是 best-effort，允许丢尾部；记忆不允许。两者语义
不同是刻意的，理由见 [runtime-boundaries.md](runtime-boundaries.md) §8。

## 4. Pi transcript

Pi session 文件是 Harness 唯一有状态的东西，落在显式声明的
`SMARTCS_PI_SESSION_DIR`（容器里是独立 volume）。先查后建：同一个 session id
不会开出第二个文件。idle 驱逐只 dispose，不删除——transcript 是用户的对话记录，
不是缓存。

## 5. 故障矩阵

以上语义由 `pi-harness/tests/f-crash-matrix.test.ts` 与
`pi-harness/tests/phase10-memory-outbox.test.ts` 用**真实 OS 进程**验收：
进程在指定窗口被 `process.abort()` 或强杀，重启后只依赖磁盘与 MySQL 的状态继续。
故障点包括 `before_llm_call`、`before_write_send`、`after_write_success`、
`before_receipt_complete`、`before_memory_enqueue`。
