# Phase 6 报告：Observability（trace 全链路传播 + span 树 + 审计管道）

> **执行方**：Claude Code（本终端，Phase 6 直接执行轮） ｜ **日期**：2026-10-04
> **依据**：`../python-impl/docs/HANDOFF-phase6.md` + `../python-impl/docs/phase6-design.md` + `pi-replatform-plan-v2.md` §6.9/§10
> **前置**：Phase 5 整体验收通过（TS 130/25 files、pytest 576/37、F1–F14 全过）
> **STATUS: completed**

---

## 0. 摘要

Phase 6 的三件事全部落地并各有实测证据：

| 目标 | 落地 | 证据 |
|---|---|---|
| 六 ID 族 + `traceparent` 全链路传播 | 每轮生成/继承 trace 上下文；**每一次** internal HTTP 调用带 `traceparent` + `x-smartcs-agent-run-id`/`-tool-call-id`/`-operation-id`；Python 侧解析并记录 | P6-1（TS 内存 exporter 与 Python in-process 记录器**同 trace_id**） |
| TS OTel span 树 | `smartcs.agent.turn` → `model.call` / `tool.call` / `compliance.review`，内存/OTLP 双 exporter，钩子内零 await | P6-1 span 树实测摘录（§3） |
| 审计管道 | `pi.on` → 有界队列 → 批量 `/internal/audit` → `migrations/004` 落库，`event_id` 幂等 | P6-2（重发不增长）、P6-3（2s 延迟不阻塞）、P6-4（溢出丢弃计数） |

**红线守住了两条**：观测管道故障不影响主链路（P6-3/P6-4 是硬门禁，且实现上钩子只做一次同步 push）；观测关闭态零回归（P6-5 + 全量基线）。

---

## 1. 交付物对照表（设计 §7）

| 设计 §7 要求 | 落点 | 状态 |
|---|---|---|
| `internal_api/audit.py` | 审计 ingest + trace 记录器 + traceparent 解析 | ✅ |
| `migrations/004_*.sql` | `audit_event` 表（设计 §3 列集原样，`CREATE TABLE IF NOT EXISTS`） | ✅ 幂等：pytest 每个测试模块、vitest 每个测试文件的重置都会重放它，全程无报错 |
| Python 用例 | `tests/test_internal_api_audit.py`（9 项） | ✅ 9 passed |
| TS `src/tracing/` | `trace-context.ts` / `provider.ts` / `spans.ts` / `audit-queue.ts`（+ 诊断渲染 `format.ts`） | ✅ |
| span 树诊断（离线可复跑） | `scripts/phase6-span-tree.ts` —— 生产对象驱动、无需 DB/模型，输出即 §3(a) | ✅ |
| 审计队列 | `AuditQueue`（有界）+ `AuditDispatcher`（≤50/批、≤1s 间隔） | ✅ |
| traceparent 传播 | `PythonInternalClient` 全部 6 类 internal 调用 | ✅ |
| `pi-harness/PHASE6_REPORT.md` | 本文件 | ✅ |
| npm 依赖锁定 | `@opentelemetry/{api 1.9.1, core 2.11.0, resources 2.11.0, sdk-trace-base 2.11.0, exporter-trace-otlp-http 0.222.0}`（`--save-exact`，已入 lockfile） | ✅ |

---

## 2. P6-1 ～ P6-6 逐项结果

| # | 场景 | 注入/观测方式 | 判定 | 结果 |
|---|---|---|---|---|
| **P6-1** | 端到端 trace 传播 | 真实 Python 子进程（trace 记录器开启）+ 真实 harness + Faux 模型；内存 exporter 读 TS span，`/internal/trace/records` 读 Python 记录 | TS `smartcs.agent.turn` 与 Python `/internal/tools/execute` 等 span **同 trace_id**、同 parent span id；六 ID 族齐全（见 §4） | ✅ |
| **P6-2** | 审计行幂等 | 同批（同 `event_id`）重发两次 → MySQL 行数不变 | 第一次 `{inserted:2,duplicates:0}`，重发 `{inserted:0,duplicates:2}`，行数恒为 2；同请求内**新** `event_id` 仍可插入 | ✅ |
| **P6-3** | 审计不阻塞主链路 | 代理对 `/internal/audit` 注入 **2s** 延迟；审计 flush 在途时发下一轮 chat | chat 结束时**审计 POST 尚未返回**（`flushSettled === false`）——即 chat 从未等待审计；审计随后照常落库（`failed=0`、行数 >0） | ✅ |
| **P6-4** | 队列溢出 | 单元：容量 3 队列推 10 条；集成：容量 1 队列跑真实 chat（每轮 ≥2 条工具事件） | 单元 `size=3 / dropped=7 / enqueued=3`，保留的是**最早的前缀** `evt-0/1/2`（溢出丢最新，审计允许丢尾但保序）；无身份事件计为丢弃并计数；真实 chat 仍 200 且回复正确、`dropped>0`，进程不崩 | ✅ |
| **P6-5** | 观测关闭态 | `OTEL_SDK_DISABLED=true` → `tracingMode()==="off"`，跑真实 chat | 答复正确、无任何 span 落内存（`getFinishedSpans()===[]`）、receipt 仍 `completed`；**另外**：默认态（无 collector → 内存）下 Phase 0–5 全部 130 项用例零回归（§10） | ✅ |
| **P6-6** | 基线 | `npx vitest run` + `python -m pytest -q` | TS **136 passed / 26 files**（Phase 5 的 130 项零回归，+6 = 本阶段新增）——由验收方在**独占环境**独立复现；pytest **585 passed / 37 skipped**（576 → +9 = 新增审计用例，本机实测）；`tsc --noEmit` 干净。本机复跑受共享测试库互斥问题干扰，见 §8 偏差 9 | ✅ |

---

## 3. span 树实测摘录

### (a) TS 侧：真实运行产物（`npx tsx scripts/phase6-span-tree.ts`）

驱动的是**生产对象本身**（`startTurnSpan` + `ChatStream` 的 `session.subscribe` 订阅路径 + 内存 exporter），只把 pi 运行时的事件流换成脚本化序列；不需要 DB/模型/Python：

```text
traceparent sent downstream: 00-8c364b2edf8d887e6fab5a78c034b2e0-77b25ad36c84d3f5-01
--- span tree ---
smartcs.agent.turn span=77b25ad36c84d3f5 (root) smartcs.session_id=phase6-demo-session smartcs.client_request_id=phase6-demo-request smartcs.agent_run_id=4242 smartcs.intent_label=order smartcs.replayed=false
  smartcs.model.call span=dec9f8e2f3c897da parent=77b25ad36c84d3f5 smartcs.model.call_index=1 smartcs.model.input_tokens=812 smartcs.model.output_tokens=36
  smartcs.tool.call span=899edf492cd1bcad parent=77b25ad36c84d3f5 smartcs.tool=order_query smartcs.tool_call_id=call_01_phase6_demo smartcs.tool.is_error=false
  smartcs.model.call span=250b3b009204f537 parent=77b25ad36c84d3f5 smartcs.model.call_index=2 smartcs.model.input_tokens=903 smartcs.model.output_tokens=52
  smartcs.compliance.review span=babc121b9f31176f parent=77b25ad36c84d3f5 smartcs.session_id=phase6-demo-session smartcs.client_request_id=phase6-demo-request smartcs.agent_run_id=4242 smartcs.compliance.verdict=pass
```

设计 §2 的三条子分支（model / tool / compliance）都在，且 parent 全部指向 turn span。

### (b) Python 侧：同一条 traceparent 下的 span 记录（生产代码路径，离线取证）

用真实依赖代码（`internal_api.audit._record_started` + 真实 service JWT 解码）解析 (a) 里那条 `traceparent`：

```json
{
  "trace_id": "8c364b2edf8d887e6fab5a78c034b2e0",
  "span_id": "d0e8fc8bd84644fe",
  "parent_span_id": "77b25ad36c84d3f5",
  "sampled": true,
  "method": "POST",
  "path": "/internal/tools/execute",
  "agent_run_id": "4242",
  "tool_call_id": "call_01_phase6_demo",
  "operation_id": null,
  "account_id": 7,
  "business_user_id": "user_002",
  "session_id": "phase6-demo-session",
  "client_request_id": "phase6-demo-request"
}
```

`trace_id` 与 TS turn span 一致，`parent_span_id` **就是** (a) 中 turn span 的 `span_id` —— 即 Python 侧 span 是 TS turn span 的子节点。

### (c) 端到端断言（P6-1，真实 chat + 真实 Python 子进程 + 真实 MySQL）

- TS `smartcs.agent.turn` 的 `traceId` == Python `/internal/tools/execute`、`/internal/auth/verify`、`/internal/context/turn-snapshot`、`/internal/compliance/review` 记录的 `trace_id`；
- 每条 Python 记录的 `parent_span_id` == turn span 的 `span_id`；
- 该轮 `agent_run_id` == receipt 行 id，且写进 receipt `response.metadata.trace_id`（replay 仍可关联）；
- 入站 `traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01` 被继承（网关可以起头）；
- 真实 live 写入场景：TS 工具 span 带 `smartcs.operation_id`，审计 `tool_result` 行的 `operation_id` 与之相同（六 ID 族补齐，见 §4）。

---

## 4. 六 ID 族传播矩阵（P6-1）

| ID | 来源 | TS span | internal HTTP | Python span 记录 | 审计行 |
|---|---|---|---|---|---|
| `trace_id` | TS 入口生成 / 继承入站 `traceparent` | turn span context（+`smartcs.trace_id` 属性） | `traceparent` 头 | ✅ 解析出的 trace_id | `audit_event.trace_id` |
| `session_id` | 请求体 / JWT | turn span 属性 + 全部子 span | JWT claim + Python 从 token 解码 | ✅ | 列 |
| `client_request_id` | 请求体 / JWT | 同上 | JWT claim | ✅ | 列 |
| `agent_run_id` | `agent_run_receipt.id` | turn span 属性 | `x-smartcs-agent-run-id` 头 | ✅ | —（不在设计列集内） |
| `tool_call_id` | Pi transcript | `smartcs.tool.call` 属性 | `x-smartcs-tool-call-id` 头 | ✅ | 列 |
| `operation_id` | Python 写账簿（**服务端在写入时生成**） | `smartcs.tool.call` 属性（写工具结果回填） | `x-smartcs-operation-id`（`/internal/operation_status` 调用） | 首次写调用**不可知**（见 §8 偏差 3） | 列 |

**关于 `operation_id` 的诚实说明**：它由运行时的写账簿在**服务端**生成，请求发出时 harness 尚不知道，因此首次 `tools/execute` 的 Python span 不可能带它。它出现在：TS 工具 span 属性（结果回填）、审计 `tool_result` 行的 `operation_id` 列、以及后续 `/internal/operation_status` 请求的 `x-smartcs-operation-id` 头。P6-1 用**真实工单写入**验证了这条链（工单落库 +1，审计行 `operation_id` 与 TS span 属性一致）。

---

## 5. 审计管道语义声明（设计 §3 要求写入报告）

- **best-effort**：进程崩溃丢队列可接受。队列有界（默认 1000，`SMARTCS_AUDIT_QUEUE_CAPACITY` 可调），溢出 **丢弃 + 计数**（`AuditQueue.dropped` / `enqueued`，经 `/health` 暴露），**不存在**向钩子回压的路径。
- **已落库行幂等不重复**：`event_id`（UUID）是唯一键，`INSERT … ON DUPLICATE KEY UPDATE id = id`，重发批次只累加 `duplicates`，永不改写既有行。
- **与记忆 outbox 的语义差异是有意的**：记忆丢失不可接受（durable，写 receipt 的 `memory_enqueue_status`，重试至上限）；审计允许尾部丢失，但**永不失真**（不重复、不篡改、身份由服务端从 token 重绑定）。
- **投递身份**：批次按 (session, client_request) 分组，每组用该轮的 service JWT（`business_user_id` 必需）+ 该轮 `traceparent` 投递；服务端再次校验 session ownership、token 与 body 的 `client_request_id` 一致、并拒收超大 payload（>16KiB，防御性，harness 端已先行截断）。
- **取不到身份的钩子事件**（在请求外执行的工具）直接丢弃并计数，绝不用别人的凭据投递。
- **可读面**：`GET /internal/trace/records` 是**默认关闭**的诊断端点（`SMARTCS_INTERNAL_TRACE_RECORDS` 打开），需 service JWT + session 归属校验（plan v2 §6.8「调试 trace 走单独 internal/admin 端点」）。

---

## 6. 观测关闭态与零回归（P6-5）

三档降级，均有代码级判定与实测：

| 配置 | 模式 | 行为 |
|---|---|---|
| `OTEL_SDK_DISABLED=1` | `off` | 不注册 provider，span 全为 no-op；**chat 行为与 Phase 5 完全一致**（P6-5 用例：答复正确、receipt `completed`、内存零 span） |
| 无 endpoint（默认） | `memory` | 有界内存 ring（1000，超出丢最旧并计数）。这是**默认态**，也是 P6-1/P6-3/P6-4 的断言面 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` 有值 | `otlp` | `BatchSpanProcessor` 异步批量导出（**不在钩子里导出**）。注意端口约定见 §8 偏差 2 |

**零回归的最强证据**：默认态就是 `memory`，因此 Phase 0–5 的全部 130 项用例是在观测开启（内存导出）的情况下跑的，**全绿**（§10）。关闭态另有一条专门用例。

---

## 7. 改动文件清单

### TS（pi-harness）
| 文件 | 性质 |
|---|---|
| `src/tracing/trace-context.ts` `provider.ts` `spans.ts` `audit-queue.ts` `format.ts` | **新增**（trace 上下文 / exporter / span 构造 / 有界队列+派发器 / span 树渲染器） |
| `scripts/phase6-span-tree.ts` | **新增**（离线 span 树诊断，§3(a) 的来源；不连 DB/模型） |
| `src/business/turn-context.ts` | `TurnIdentity` += `traceparent` / `agentRunId`（可选，向后兼容） |
| `src/business/python-client.ts` | 全部 internal 调用带 trace 头；`executeTool` += `toolCallId`；`operationStatus` 带 operation 头；新增 `postAudit` |
| `src/agent/extensions/audit.ts` | 记录富化（trace/session/request/operation）、列表加界、队列投递；钩子内仍**只做同步 push** |
| `src/agent/create-smartcs-agent.ts` | `auditQueue` 选项；审计扩展接入 `TurnContext`；合规评审 span |
| `src/agent/tools/business-tools.ts`、`shadow-write-tools.ts` | 透传 Pi 的 `toolCallId` |
| `src/streaming/chat-stream.ts` | 订阅路径产生 model/tool 子 span（含内存泄漏兜底：`finish()` 关闭悬挂 span） |
| `src/server/chat-pipeline.ts` | 每轮 turn span、traceparent 贯穿、`trace_id` 写入 receipt `response.metadata`、replay/账簿恢复路径同样纳入 span |
| `src/server/app.ts` | 读取入站 `traceparent` |
| `src/server/main.ts` | `initTracing()` + 审计队列/派发器装配与停止 |
| `package.json` / `package-lock.json` | 5 个 OTel 依赖（精确版本） |
| `tests/phase6-observability.test.ts` | **新增**（P6-1/3/4/5） |
| `tests/helpers/phase1.ts` | 迁移并入 004；`traceRecords` 选项（默认关，与生产一致） |

### python-impl
| 文件 | 性质 |
|---|---|
| `internal_api/audit.py` | **新增**（审计 ingest + trace 记录器 + traceparent 解析 + 受门禁的诊断读） |
| `migrations/004_phase6_audit.sql` | **新增** |
| `tests/test_internal_api_audit.py` | **新增**（9 项） |
| `internal_api/__init__.py` | **修改**：注册 audit 路由 + 给全部 internal 路由挂 trace 依赖（与 5c 注册 `operation_status` 同一手法） |
| `tests/internal_api_helpers.py` | **修改**：`MIGRATIONS` 并入 003/004、drop `audit_event` |

`tracing/`、`agents/`、`mcp/`、`memory/`、`context/`、`rag/`、`auth/`、`tickets/`、`refunds/`、`api/` **本轮零改动**。

---

## 8. 偏差与限制（如实记录）

1. **Python 侧 span 的实现方式（设计允许的取舍）**：设计 §1 允许「FastAPI 自动插桩优先；若未启用则用轻量 in-process 记录器」。生产 app（`api/main.py`）确实装了 `FastAPIInstrumentor` + 全局 W3C propagator，会自动把 `traceparent` 解成父子关系；但**测试用的 inline app 没有装插桩**，为了 P6-1 有可断言的 Python 侧证据，本阶段在 `internal_api/audit.py` 内实现了**轻量 in-process 记录器**（有界 deque，500 条），对每个 internal 请求记录一条含六 ID 与 parent span id 的记录。它同时**best-effort 地把同样的属性写到当前 OTel span**（若存在），与自动插桩叠加而非替代。记录器全程 try/except 包裹，任何失败都降级为「没有记录」，绝不改变响应。
2. **TS 的 OTLP exporter 是 HTTP/protobuf，不是 gRPC**：`@opentelemetry/exporter-trace-otlp-http` 需要 HTTP 端口（惯例 4318，路径 `/v1/traces`，代码会在 endpoint 后自动补 `/v1/traces`）；Python 侧现有 `init_tracer` 用的是 gRPC exporter（惯例 4317）。两者都能进同一个 collector，但**部署时必须配对正确的端口/协议**，否则 TS 侧导出会静默失败（异步、best-effort，不影响主链路）。
3. **首次写调用的 `operation_id` 不可知**（见 §4）：它由服务端在写入时生成，因此首跳的 Python span 与 `x-smartcs-operation-id` 头里没有它。这是信息论限制，不是遗漏；补偿点是 TS 工具 span 属性、审计 `tool_result` 行的 `operation_id` 列、以及后续 reconcile 调用。
4. **合规 span 由 `traceparent` 挂父**：合规评审发生在 `pi.on("message_end")` 内，拿不到 pipeline 的 span 对象，因此用该轮的 `traceparent` 构造子 span（同一 trace、同一父 span id）。语义等价，但实现路径与 model/tool span 不同（后者直接持有父 span）。
5. **内存 exporter 有界（1000）**：默认态不会无限增长；超出丢最旧并计数（`getDroppedSpanCount()`）。因此**长跑进程的早期 span 会被淘汰**——这是刻意的，内存模式是诊断/测试面，不是长期存储。
6. **`x-smartcs-trace-id` 响应头**：internal 响应会回显 Python 观察到的 trace id（便于日志关联）。它在依赖的「pre-yield」阶段设置（FastAPI 只在调用前合并依赖注入的 Response 头），因此**仅成功响应**携带该头；错误响应不带（不影响主链路）。
7. **P6-3 的时序断言**：用「审计 POST 尚未返回时 chat 已完成」作为不阻塞的判据（`flushSettled === false`），比纯延迟阈值更稳；延迟阈值放宽到 <2s 仅为粗护栏。
8. **`tests/internal_api_helpers.py` 的 MIGRATIONS 补入 003**：此前 Python 侧从不应用 003，Phase 5 用例实际依赖「TS 套件曾跑过」这一隐藏前提。本轮补上（004 也必须进同一列表），使 Python 套件可独立复跑。这是测试基础设施修复，不涉及产品代码。
9. **共享测试库互斥（本阶段实测到的环境事故，非产品缺陷）**：`python-impl/tests`（pytest）与 `pi-harness/tests`（vitest）**共用 `smartcs_phase1_test`**，而两边的 fixture 都用「DROP + CREATE + 迁移」重置该库。本机同时存在两个 agent 会话（执行方 / 验收方），并发跑套件时互相打断，表现是**假的**失败：`Failed to open the referenced table 'platform_user'`（父表被对方 DROP 后，本方在建带 FK 的子表）、`Duplicate column name 'memory_attempts'`（两侧同时跑迁移 002 的 TOCTOU）、`Table 'platform_user' already exists`、以及测试中途 seed 被抹掉导致的 403/404。用探针证实过：两个不同 worker 进程同时 seed 同一批账号并抢占同一端口。**干扰只会造成假失败、不会造成假通过**——所以验收方在独占环境复现的 136/136 是有效证据。已商定纪律：动 `smartcs_phase1_test` 的套件必须先在终端声明占用窗口；中期方案是按 runner 分离库名（fixture 已支持 `SMARTCS_TEST_DATABASE`）并让 `seedAccount` 幂等。
10. **验收后的一处非行为性改动**：验收方独立复现 136/136 之后，我新增了 `src/tracing/format.ts`（span 树渲染器）与 `scripts/phase6-span-tree.ts`（离线 span 树诊断，即 §3(a) 的来源），并把 `tests/phase6-observability.test.ts` 里仅在 `SMARTCS_DUMP_SPANS=1` 时生效的 dump 辅助改为复用该渲染器。**不改变任何断言语义、默认路径不执行**；`tsc --noEmit` 干净。如需在干净窗口复跑确认，随时可安排。

---

## 9. Python 现有 `tracing/` 覆盖度（只读，如实说明）

- `api/main.py` 已装 `FastAPIInstrumentor.instrument_app(app)`（`opentelemetry-instrumentation-fastapi`，requirements 已含）→ **internal_api 的 HTTP server span 由自动插桩产生**，`traceparent` 经全局 W3C propagator 自动建立父子关系。本阶段未改该文件。
- `tracing/observability.py` 的 `InstrumentedToolExecutor` / `InstrumentedExecutionReconciler` 提供的是**结构化日志 + 进程内指标**，**不是 OTel span**：**ledger/domain/DB 子 span 目前不存在**。设计 §4 允许「若现有插桩已覆盖则直接受益」，实测结论是**部分覆盖**（HTTP 层有，ToolExecutor 内部无）。因 `tracing/` 本阶段只读，未新增子 span——如实报告，不声称已覆盖。
- `tracing/otel_config.py::init_tracer` 在 `OTEL_EXPORTER_OTLP_ENDPOINT` 有值时使用 **gRPC** exporter。本机 `python-impl/.env` 配了 `OTEL_EXPORTER_OTLP_ENDPOINT` 但无 collector 在听，pytest 收尾会打印一次 `Failed to export traces to localhost:4317` ——这是**既有环境行为**（本轮未改 `.env`、未改 tracing），与 Phase 6 记录器无关（记录器不依赖任何 exporter）。

---

## 10. 基线与机器纪律

```
$ npx vitest run          # 由验收方在独占环境复现（本机复跑见偏差 9）
 Test Files  26 passed (26)
      Tests  136 passed (136)          # 对照 Phase 5 终态：25 files / 130 tests → +1 file / +6 tests，零回归

$ python -m pytest -q     # 本机实测
585 passed, 37 skipped, 1 warning in 294.80s   # 对照 576/37 → +9（新增审计用例），基线未破

$ npx tsc --noEmit
（无输出，干净）
```

> **本机复跑的实况（如实记录）**：在共享库被对方套件同时使用的窗口里，我这边出现过 `Test Files 3 failed | 23 passed`、`122 passed | 14 skipped`，三处失败全部是 `resetTestDatabase` 里的 FK/DDL 竞态（签名见偏差 9），**没有一处落在断言上**；单独跑这些文件（`phase2-tools` 7/7、`phase1-e2e`+`phase2-tools` 18/18）全绿。验收数字以独占环境为准。

| 项 | 状态 |
|---|---|
| 不 commit / 不 push | ✅ HEAD 仍 `abf71d2` |
| 机器纪律 | ✅ 无残留 python/node 测试进程；诊断探针已还原（`tests/helpers/phase1.ts` 仅保留 Phase 6 的正当改动） |
| python-impl 白名单 | ✅ 业务目录零改动（`api/`、`agents/`、`mcp/`、`memory/`、`context/`、`rag/`、`auth/`、`tickets/`、`refunds/`、`tracing/` 全部未改） |
| 新增依赖锁版本 | ✅ 5 个 OTel 包精确版本入 `package.json` 与 lockfile |
| 未降级表述 | ✅ 未执行/未覆盖项明确标注（§8 偏差 1/3、§9 覆盖度） |

---

## 11. 合规自查（对照交接指令 §2）

| 约束 | 状态 |
|---|---|
| 观测管道故障不得影响主链路（pi.on 内禁 await exporter/网络） | ✅ 钩子内只有同步对象构造 + 数组 push；导出一律由 SDK 处理器（batch）或内存 ring 承担；P6-3/P6-4 双向验证 |
| 审计队列有界（溢出丢弃+计数） | ✅ 默认 1000，`dropped`/`enqueued` 计数经 `/health` 暴露；P6-4 实测 |
| 观测关闭态零回归（P6-5） | ✅ 专项用例 + 130 项既有用例零回归 |
| 不改业务语义 | ✅ 全部改动为旁路；唯一进入响应的是诊断头与 receipt metadata 的新字段（additive） |
| python-impl 白名单 | ✅ 新增 `internal_api/audit.py`、`migrations/004`、`tests/test_internal_api_audit.py`；**修改** `internal_api/__init__.py`（路由+依赖注册）与 `tests/internal_api_helpers.py`（迁移列表），均在 `internal_api/`、`tests/` 范围内，已如实记录（§7/§8.8） |
| `tracing/` 只读 | ✅ 未改一行；覆盖度如实报告（§9） |
| 报告续写不覆盖 | ✅ 本文件为新建（Phase 6 首轮），未触碰 PHASE0–5 报告 |

---

**STATUS: completed**

- **P6-1 ~ P6-6 全部实现并各有可复跑用例**：trace 全链路同 trace_id（含入站继承）、六 ID 族（写场景由真实工单落库补齐 `operation_id`）、审计幂等/不阻塞/溢出计数、观测关闭态零回归
- **新增用例**：TS +6（`tests/phase6-observability.test.ts`）、Python +9（`tests/test_internal_api_audit.py`）
- **基线**：TS **136 passed / 26 files**（130 零回归）；pytest **585 passed / 37 skipped**；`tsc` 干净
- **红线**：观测管道故障不影响主链路、关闭态零回归，均有硬门禁用例
- 未 commit / 未 push；HEAD `abf71d2`

PHASE6_DONE completed

---
---

# Phase 6b 修复轮（测试基础设施）

> **执行方**：Claude Code（本终端） ｜ **日期**：2026-10-04
> **依据**：`../python-impl/docs/HANDOFF-phase6b.md`
> **STATUS: completed**

> 上文 Phase 6 章节**原样保留**，未覆盖。本轮只动测试基础设施：**业务代码零改动**（`api/`、`agents/`、`mcp/`、`memory/`、`context/`、`rag/`、`auth/`、`tickets/`、`refunds/`、`tracing/` 全部未改；`internal_api/` 本轮也未改）。

## 6b.1 修复对照

| # | 交接要求 | 落点 | 验证 |
|---|---|---|---|
| 1 | fixture 凭据自举 | `tests/helpers/phase1.ts` 新增 `childMysqlEnv()`：用**与测试进程相同的规则**（`process.env` 优先 → `python-impl/.env` 回退，复用 `src/config/env.ts` 的 `envValue`）解析 `MYSQL_HOST/PORT/USER/PASSWORD/DATABASE`，并**显式并入** Python 子进程 env；解析失败即在 fixture 抛错并指名变量 | §6b.3 反证 ① / §6b.2 |
| 1b | 静默路径 fail-fast | inline Python 服务在 `SMARTCS_WRITE_MODE=live` 时于 startup 探 `platform_user / conversation_session / agent_run_receipt / pending_action`，缺表即 `RuntimeError` 拒绝启动（错误信息带 `MYSQL_DATABASE` 名）；同时 fixture 增加**子进程早退检测**，启动失败毫秒级报错并附 stderr，不再干等 30s deadline | §6b.3 反证 ② |
| 2 | 测试库按 runner 分离 | 两侧统一由 `SMARTCS_TEST_DATABASE` 决定库名（TS：`tests/helpers/phase1.ts`；Python：`tests/internal_api_helpers.py`）；`tests/phase1-restart.test.ts` 里重复的库名字面量改为复用 `TEST_DATABASE` 常量；Python 新增 `platform_database()` 并让 `build_app` 使用——**钉死测试库**，杜绝「模块忘写 monkeypatch 就打到 `.env` 的服务库 `smartcs_checkpoint`」这一同类隐患 | §6b.5 + `tests/README.md` |
| 3 | seedAccount 幂等 | `seedAccount` / `seedSession` 改为**先清自身残留再插入**（`username` / `business_user_id` / `session_id` 三个唯一键；会话行先删，尊重 `session_account_fk`） | §6b.4 定向验证 |
| 4 | 占用窗口纪律落文档 | 新建 `pi-harness/tests/README.md`：共享库事实、窗口纪律、按 runner 分离库名的用法、凭据解析规则、seed 幂等说明 | 文件存在 |

## 6b.2 验收证据：干净 shell（unset 凭据）下全量 vitest 全绿

```bash
$ env | grep -c '^MYSQL_'                 # 干净 shell 的前提：无任何 MYSQL_* 注入
0

$ env -u MYSQL_PASSWORD -u MYSQL_HOST -u MYSQL_USER -u MYSQL_PORT sh -c 'npx vitest run'
 Test Files  27 passed (27)
      Tests  138 passed (138)             # Phase 6 的 136 项 + 本轮新增 2 项
```

（凭据全部由 fixture 从 `python-impl/.env` 解析后显式下发给子进程；上表两行均为本轮实测。）

对照：修复前的干净 shell 会走到「Python 服务能用 .env 凭据起来、但配置错位时静默降级」的路径；现在凭据解析、下发与 schema 自检都在 fixture 里显式完成。

## 6b.3 反证：fail-fast 的真实输出（两条都是实测）

**① 凭据完全解析不到**（临时探针：`SMARTCS_PYTHON_ENV_FILE` 指向不存在的文件 + 无 `MYSQL_PASSWORD`）：

```text
fixture threw: test fixtures need MySQL credentials: export MYSQL_PASSWORD or set it in python-impl/.env
cause: Error: MYSQL_PASSWORD is required
```

**② live 模式下看不到平台库**（指向不存在的库名）：

```text
after 13352ms the fixture threw:
python internal_api exited during startup (code=3): <string>:12: DeprecationWarning: on_event is deprecated...
ERROR:    Traceback (most recent call last):
  ...
  File "<string>", line 15, in _startup
    await db.initialize()
RuntimeError: SMARTCS_WRITE_MODE=live requires the platform schema in MYSQL_DATABASE=smartcs_phase6b_missing
  (platform_user/conversation_session/agent_run_receipt/pending_action): PlatformUnavailable(...)
```

（若没有早退检测，这里会白等满 30s deadline 再抛一句无从下手的 "did not start"。）

## 6b.4 seed 幂等性：定向验证

临时探针直接调用 helper（用完即删），三种残留形态：

```text
same owner twice: id 1 → 2          # 同 username+business_user_id 重复 seed：成功（替换）
business_user_id collision: id 3    # 新 username 撞已有 business_user_id：成功（替换）
same session twice: ok              # 同 session_id 重复 seed：先删会话行再插（FK 安全）
platform_user rows: [{"id":3,"username":"p6b-owner-2","business_user_id":"user_p6b"}]
```

即：残留不再引发 `Duplicate entry '…'` 的**级联假失败**；库中最终恰好一行、值正确。
另附回归：同一个 f-matrix 文件**连跑两次**均 `6 passed`（第二次带着第一次的残留）。

## 6b.5 本轮改动文件

| 文件 | 性质 |
|---|---|
| `pi-harness/tests/helpers/phase1.ts` | `childMysqlEnv()`；Python 子进程 env 显式并入 `MYSQL_*`；子进程早退检测；startup schema 自检（inline 程序内）；`seedAccount`/`seedSession` 幂等 |
| `pi-harness/tests/phase1-restart.test.ts` | 复用 `TEST_DATABASE` 与 `childMysqlEnv(TEST_DATABASE)`，删除重复库名字面量 |
| `pi-harness/tests/phase6b-fixture-discipline.test.ts` | **新增**（2 项：live 模式缺 schema 拒绝启动 / 凭据解析规则） |
| `pi-harness/tests/README.md` | **新增**（共享库窗口纪律 + runner 分离 + 凭据 + seed 幂等） |
| `python-impl/tests/internal_api_helpers.py` | 新增 `platform_database()`（钉死测试库）；`build_app` 改用它 |

## 6b.6 偏差与限制

1. **`internal_api/tools.py` 的静默降级未改**（业务代码零改动是本轮硬约束）：`refund_evaluate` 在 `platform_database is None` 时会跳过 `pending_action`，随后由第二段确认自然拒绝。生产语义上这是「优雅降级」，本轮**保留**；测试侧改由启动自检 fail-fast，使测试路径不可能再落到该分支。若规划方希望生产侧也改为显式报错，那是一次业务行为变更，应单独排期。
2. **seed 是「替换」而非「合并」**：重复 seed 会得到**新的 account id**（先删后插）。现有用例都是「seed 一次、用返回的 id」，不受影响；若将来有人 seed 同一账号两次并复用旧 id，需要改成 upsert 语义——已在 `tests/README.md` 写明唯一键与残留处理方式。
3. **`SMARTCS_TEST_DATABASE` 仍是进程级（import 时求值）**：两侧都在模块加载时读一次，因此必须在启动 pytest/vitest **之前**设定。尚未做成运行时可切换（无此需求）。
4. **按 runner 分离库名需要 DBA 先建库**：仓库的 MySQL 用户只有 `smartcs_checkpoint` 与 `smartcs_phase1_test` 的授权（实测 `CREATE DATABASE` 被拒），所以 `smartcs_claude_verify` 这类新库名必须由有权限的一方创建后才能使用。
5. **启动自检有成本**：live 模式下每个 Python 测试服务多 4 条 `SELECT 1 … LIMIT 1`，可忽略；但它换来的是「配置错位当场响」。

## 6b.7 基线（本轮实测）

```text
$ env -u MYSQL_PASSWORD … npx vitest run        # 干净 shell
 Test Files  27 passed (27)
      Tests  138 passed (138)

$ env -u MYSQL_PASSWORD python -m pytest -q
585 passed, 37 skipped, 1 warning in 283.64s    # 与 Phase 6 一致，未破

$ npx tsc --noEmit
（无输出，干净）
```

| 项 | 状态 |
|---|---|
| 业务代码零改动 | ✅ 仅 `pi-harness/tests/*`、`pi-harness/tests/README.md`、`python-impl/tests/internal_api_helpers.py` |
| 不 commit / 不 push | ✅ HEAD 仍 `abf71d2` |
| 机器纪律 | ✅ 无残留进程；两枚临时探针脚本已删除；占用窗口期间未与验收方并发 |
| 未降级表述 | ✅ 两条 fail-fast、seed 幂等均为**实测输出**，非推断 |

---

**STATUS: completed**

- **修复项 1/1b/2/3/4 全部落地并各有实测证据**：凭据自举 + 启动 fail-fast（两条反证）、库名按 runner 统一、seed 幂等（三种残留形态）、窗口纪律文档
- **验收线**：干净 shell（0 个 `MYSQL_*`）下全量 vitest **138 passed / 27 files**；pytest **585 passed / 37 skipped**；`tsc` 干净
- 业务代码零改动；未 commit / 未 push；HEAD `abf71d2`

PHASE6B_DONE completed
