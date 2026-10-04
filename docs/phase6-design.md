# Phase 6 详细设计：Observability（定稿 v1）

> **状态**：定稿（Phase 5 整体验收通过后下发） ｜ **日期**：2026-10-04
> **依据**：`pi-replatform-plan-v2.md` §6.9/§10 Phase 6。
> **范围红线**：不改任何业务语义；观测管道自身故障不得影响主链路（观测永远 best-effort，主链路永远优先）。

---

## 1. ID 族与 trace 传播

```text
每轮传播（v2 §6.9）：trace_id · session_id · client_request_id · agent_run_id · tool_call_id · operation_id
```

- TS 请求入口生成（或从上游 `traceparent` 继承）`trace_id`（W3C 兼容格式），写入 receipt `response.metadata` 并以 `traceparent` 头传给**每一次** internal HTTP 调用（auth/tools/context/memory/compliance/operation_status）。
- `agent_run_id` = receipt 行 id；`tool_call_id`/`operation_id` 已有来源（Pi transcript / open_write_operations）。
- Python 侧 internal_api 从 `traceparent` 头解析并挂到当前 span（FastAPI 已有 OTel 自动插桩可用；若未启用则用轻量 in-process 记录器，报告说明取舍）。

## 2. TS 侧 span（pi 事件 → OTel）

- 引入 `@opentelemetry/api` + 内存/OTLP 双 exporter（`OTEL_EXPORTER_OTLP_ENDPOINT` 有值走 OTLP，否则内存收集——测试用内存断言）。
- span 结构：
```text
smartcs.agent.turn            (attributes: session_id, client_request_id, agent_run_id, intent_label)
  ├ smartcs.model.call        (from message_start/end)
  ├ smartcs.tool.call         (tool_execution_start/end; attrs: tool, toolCallId, operation_id, isError)
  └ smartcs.compliance.review (message_end 合规调用)
```
- **钩子零阻塞红线**：span 上报走 fire-and-forget（OTel SDK 自带批量异步导出，禁止在 pi.on 里 await exporter）。

## 3. 审计管道（v2 §6.9 落地）

```text
TS 扩展 pi.on("tool_call"/"tool_result") → 有界内存队列（背压：溢出丢弃+计数）
  → 批量 POST /internal/audit（batch ≤ 50，间隔 ≤ 1s）
  → Python internal_api/audit.py 校验 service JWT → 落 MySQL audit 表
```

```sql
-- migrations/004_phase6_audit.sql
audit_event: id BIGINT PK AI · event_id CHAR(36) UNIQUE（幂等键，重复批次忽略）
  session_id · client_request_id · kind ENUM('tool_call','tool_result')
  tool_name · tool_call_id · operation_id · payload JSON
  trace_id · created_at · KEY(session_id, created_at) · KEY(trace_id)
```

**语义声明（写入报告）**：审计是 best-effort——进程崩溃丢队列可接受（溢出/丢弃计数暴露给指标），但**已落库行幂等不重复**。与记忆 outbox（durable 保证）的语义差异是**有意的**：记忆丢失不可接受，审计允许尾部丢失但永不失真。

## 4. Python 侧 span

- internal_api 各端点挂 span（FastAPI 自动插桩优先；手动 span 兜底），属性含六 ID 族。
- ToolExecutor 内部（ledger/domain/DB 子 span）**不改动现有 tracing 代码**——若现有插桩已覆盖 internal_api 路径则直接受益，报告如实说明覆盖度。

## 5. 白名单

python-impl 可写**新增**：`internal_api/audit.py`、`migrations/004_*.sql`、对应 `tests/`。TS 侧新增 `src/tracing/`。其余只读约束不变。

## 6. 验收用例（P6-1～P6-6）

| # | 场景 | 必须保证 |
|---|---|---|
| P6-1 | 端到端 trace 传播 | 内存 exporter 断言：TS turn span 与 Python internal span **同 trace_id**，六 ID 族属性齐全 |
| P6-2 | 审计行幂等 | 同批重发 → `event_id` 唯一键去重，行数不变 |
| P6-3 | 审计不阻塞主链路 | Python audit 端点延迟注入 2s → chat 端到端延迟不受影响（fire-and-forget 证据） |
| P6-4 | 队列溢出 | 注入超量工具事件 → 溢出丢弃计数 > 0，进程不崩、主链路正常 |
| P6-5 | 观测关闭态 | 无 OTEL endpoint 时全部降级为内存/无操作，行为与 Phase 5 完全一致（零回归） |
| P6-6 | 基线 | TS 全绿（含 Phase 5 的 130 项零回归）+ pytest ≥ 576 |

## 7. 交付物

`internal_api/audit.py` + `migrations/004` + 用例；TS `src/tracing/` + 审计队列 + traceparent 传播；**`pi-harness/PHASE6_REPORT.md`**（对照表、P6-1～P6-6、span 树实测摘录、审计语义声明、偏差、`STATUS:`、终行完成标记）。
