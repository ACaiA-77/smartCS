# Phase 3 报告：Context / Memory / Compliance

> **执行方**：ds-for-act（本终端） ｜ **日期**：2026-10-04
> **依据**：`../python-impl/docs/HANDOFF-phase3.md` + `../python-impl/docs/phase3-design.md`（已定稿）
> **范围**：不接 WRITE、不建 pending_action、不拆 RAG、compose 未动；未 commit / 未 push。
> **STATUS: completed**

---

## 0. 结论速览

| 项 | 结论 |
|---|---|
| 交付物 | `internal_api/{context,memory,compliance}.py`、`migrations/002_*`、TS prefetch/dispatcher/合规扩展/RunOutputBuffer/compaction |
| 设计 §7 验收 | **P3-1 ～ P3-9 全部通过**（P3-9 见 §3） |
| TS 测试 | **101 passed / 0 failed**（19 文件），`tsc --noEmit` 干净 |
| pytest 基线 | **559 passed / 37 skipped**（Phase 2 基线 540 → **+19**，正是本阶段新增用例） |
| 只读目录 | `agents/`、`context/`、`memory/`、`mcp/`、`rag/`、`auth/`、`docker-compose.yml` **零改动** |
| 机器纪律 | 全部测试串行执行；未起长时后台模型任务；结束时**无 3GB+ python 进程残留** |
| 偏差 | 4 项，其中 **D1 需裁决**（记忆 provenance 数据源）；未静默改文档 |

---

## 1. 实现对照 design §1–§7

| 设计节 | 要求 | 落地 | 证据 |
|---|---|---|---|
| §1 组件职责 | 快照服务 / outbox / 合规服务 / 注入扩展 / RunOutputBuffer / compaction | 全部就位（见 §1.1） | — |
| §2 Turn Snapshot | 契约、prompt 前 prefetch、pi.on 内不 await 网络、`protected_fields` 来自 Python 权威 | `internal_api/context.py` + `extensions/context-injection.ts`（同步注入）+ 管线 prefetch | `tests/test_internal_api_context.py`（5）；P3-1/P3-8 |
| §3 Durable Outbox | receipt 增列、dispatcher 扫描、透传 provenance 回查、kill -9 后可补投 | `migrations/002` + `session/outbox.ts` + `internal_api/memory.py` | `tests/test_internal_api_memory.py`（6）；P3-3 |
| §4 Compliance 与 final | 规则必走 + LLM 可配、replacement 写回、settled 后发 final、text_delta 不直发 | `internal_api/compliance.py` + `extensions/compliance.ts` + `ChatStream` | `tests/test_internal_api_compliance.py`（8）；P3-4/5/6/7 |
| §5 compaction | 默认策略 + modelOverrides、快照不受 compaction 影响、事件入 metadata | `create-smartcs-agent.ts` 的 compaction profile；`ChatStream.compactionEvents` → receipt metadata | P3-1(F10) |
| §6 白名单 | 仅新增三个 internal_api 文件 + migrations/002 + 对应 tests | `git status` 实测 | §2 |
| §7 验收 | P3-1 ～ P3-9 | 见 §3 | — |

### 1.1 新增/变更文件

```text
python-impl/
├─ internal_api/context.py        ← 新：POST /internal/context/turn-snapshot
├─ internal_api/memory.py         ← 新：POST /internal/memory/enqueue（+ D1 适配器）
├─ internal_api/compliance.py     ← 新：POST /internal/compliance/review
├─ internal_api/auth.py           (改：抽出 resolve_service_session 共用前置校验)
├─ internal_api/__init__.py       (改：合并三个新子路由)
├─ migrations/002_phase3_memory_outbox.sql  ← 新
├─ api/main.py                    (+4 行：app.state.order_repository / user_memory_service)
└─ tests/{test_internal_api_context,memory,compliance}.py + helpers 扩展

pi-harness/src/
├─ agent/extensions/context-injection.ts   ← 新：SnapshotHolder + 同步注入扩展
├─ agent/extensions/compliance.ts          (改：可插拔 reviewer)
├─ agent/create-smartcs-agent.ts           (改：快照 holder / reviewer / compaction profile)
├─ session/outbox.ts                       ← 新：MemoryOutboxDispatcher
├─ session/registry.ts                     (改：SessionRuntime 携带 snapshotHolder)
├─ streaming/chat-stream.ts                (改：记录 compaction 事件)
├─ server/chat-pipeline.ts                 (改：snapshot prefetch + compaction metadata)
└─ business/python-client.ts               (改：fetchTurnSnapshot / enqueueMemory / reviewCompliance)
```

---

## 2. python-impl 改动范围（白名单合规自查）

```
 M .env.example          (Phase 1 起既有)
 M api/main.py           (+4 行：两个只读数据源挂到 app.state)
?? internal_api/         (Phase 1 新增；本阶段加 context.py / memory.py / compliance.py)
?? migrations/           (Phase 1 新增；本阶段加 002)
?? tests/test_internal_api_{context,memory,compliance}.py 等
```

**只读目录实测零改动**：`agents/`、`context/`、`memory/`、`mcp/`、`rag/`、`auth/`、`docker-compose.yml`。
HEAD 仍为 `abf71d2`，未 commit / 未 push。

---

## 3. 设计 §7 验收用例结果表（P3-1 ～ P3-9）

全部为**真实 MySQL + 真实 Python 运行时子进程 + 真实 Pi 运行时**，仅模型为 Faux。

| # | 场景 | 结果 | 证据 |
|---|---|---|---|
| P3-1 | 快照注入 + compaction 后仍注入（F10） | ✅ PASS | `tests/phase3-context-compliance.test.ts`：断言 transcript 含快照块，且**来自 Python 权威**的真实订单 `ORD-20260801-0081` 与状态；第二轮再次注入（计数 +1），**每轮独立于历史**——这正是 F10 的机制保证 |
| P3-2 | 记忆不串用户 | ✅ PASS | TS 侧：快照文本不含 `user_002`、不含其订单 `ORD-20260801-0002`；Python 侧：`test_internal_api_context.py` 用真实仓库逐条验证「每个出现的订单都属于当前用户、且不属于他人」，outbox 侧 `test_internal_api_memory.py` 验证他人 event 无法入队（404） |
| P3-3 | outbox 崩溃安全 | ✅ PASS | `test_internal_api_memory.py` 覆盖幂等（重复入队不产生重复 candidate）；TS 侧：一次 dispatch 后 receipt 置 `done`，**新建 dispatcher（等价重启）再扫描时 `scanned=0 / enqueued=0`**——投递状态由 MySQL 决定而非进程内存 |
| P3-4 | PII 先流出后替换 = 不可能 | ✅ PASS | 模型输出含 `13800138000`：**用户收到的 final** 与 **transcript 所存** 均为 `138*****000`，两处都不含原文 |
| P3-5 | text_delta 永不外发 | ✅ PASS | 解析全部 SSE `data:` 帧：帧类型仅 `status`/`final`/`done`，**恰好一个 `final` 且内容为完整答复**，其前没有任何承载助手文本的帧 |
| P3-6 | sanitize 状态一致 | ✅ PASS | 断言「用户收到 == transcript 所存 ==」为同一字符串（P3-4/P3-6 同一用例双侧断言） |
| P3-7 | fail 兜底 | ✅ PASS | 模型输出「保证收益、零风险、稳赚不赔」→ 用户收到确定性兜底文案（含「未能通过合规检查」、不含违规词），**receipt 照常 completed** |
| P3-8 | protected_fields 权威 | ✅ PASS | 模型声称订单「已取消」，而快照块以「权威业务事实（以此为准）」注入仓库中的真实状态；断言快照携带真实订单事实且被显式标注为权威来源（不依赖 transcript） |
| P3-9 | 基线 | ✅ PASS | `python -m pytest -q` → **559 passed / 37 skipped**（540 → +19）；`npx vitest run` → **101 passed**；`tsc --noEmit` 干净。**全部串行执行** |

---

## 4. 测试运行命令与最终结果

### 4.1 命令

```bash
# ---- Harness（TS）----
cd D:/Workspace_for_Codex/project005_SmartCS/pi-harness
npm ci
npx tsc --noEmit
npx vitest run                                   # 串行；vitest 默认 fileParallelism:false
npx vitest run tests/phase3-context-compliance.test.ts   # P3-1 ~ P3-8

# ---- Python（不要与任何模型任务并发）----
cd D:/Workspace_for_Codex/project005_SmartCS/python-impl
python -m pytest -q
python -m pytest tests/test_internal_api_context.py tests/test_internal_api_memory.py tests/test_internal_api_compliance.py -q
```

**前置条件**：compose MySQL `:3307` 在运行；测试库 `smartcs_phase1_test` 存在且 `smartcs` 用户已授权。
`tests/internal_api_helpers.py` 会 DROP/CREATE 该库的表并**依次执行 migrations 001 + 002**，不触碰 `smartcs_checkpoint`。

### 4.2 最终结果

```
$ npx tsc --noEmit
(无输出 = 干净)

$ npx vitest run
 Test Files  19 passed (19)
      Tests  101 passed (101)

$ python -m pytest -q
559 passed, 37 skipped, 1 warning in 200.90s

# 基线对照
Phase 2 : 540 passed / 37 skipped
Phase 3 : 559 passed / 37 skipped   (+19 = context 5 + memory 6 + compliance 8)
```

### 4.3 机器纪律（新增第 6 条）执行情况

| 要求 | 执行 |
|---|---|
| 测试一律串行 | ✅ `vitest.config.ts` 的 `fileParallelism: false`；pytest 天然串行；**本阶段未并发跑模型任务** |
| 禁止长时后台模型任务 | ✅ 本阶段**未启动任何 benchmark 类后台任务** |
| 结束前确认无 3GB+ python 残留 | ✅ `tasklist | findstr python` → **无匹配进程** |

---

## 5. 关键实现说明

- **快照注入为何不违反「pi.on 内禁止 await 网络」**：快照在 `prompt()` **之前**由管线 prefetch（`chat-pipeline.ts`），扩展 `before_agent_start` 只把**已在内存中**的 blocks 格式化后返回。钩子内没有任何 I/O。
- **快照无法与身份解析完全并行**：Service JWT 需要 `business_user_id`，而它正是身份解析的产物，因此顺序是「身份解析 → 快照 prefetch → prompt」。设计写的是「与身份解析并行」，实测该并行不可达；但设计要求真正要保的性质（**在 prompt 前完成、不在钩子里等网络**）已满足。记 D2。
- **合规复核确实 await 网络**：`message_end` 内调用 `/internal/compliance/review`。这是 plan v2 §6.5 的明确设计（final 已在关键路径上，属 parity），与 §6.6 针对**记忆/快照**注入的「钩子禁慢 I/O」是两条不同约束。记 D3。
- **规则表单一来源**：`internal_api/compliance.py` **直接 import** `agents/compliance_checker.py` 的 `SENSITIVE_PATTERNS` / `FORBIDDEN_TERMS`，未复制、未新建 `compliance_rules.py`，因此 `agents/` 保持零改动且规则不会漂移。
- **compaction**：默认开启并设 `reserveTokens=8192` / `keepRecentTokens=16384`；`compaction_*` 是 subscribe 独有事件，由 `ChatStream` 记录进 receipt 的 `metadata.compaction_events`（观测用）。

---

## 6. 偏差节（均未静默改文档）

### D1（**需裁决，影响功能语义**）— 记忆 provenance 的数据源与计划不符

**事实**：计划 v2 §6.6 指定 `memory_source_event` 为本路径的 durable provenance，Phase 1 也为此建了该表；但 `memory/MySQLUserMemoryRepository.source_event()` 实际读取的是 **`conversation_event` JOIN `session_digest`**（legacy checkpoint 事件日志），且要求 `event_id` 为**数字**。Pi 路径不写这两张表 → 纯透传实现下**每次入队都返回 404 / provenance 失败**，outbox 永远无法投递。

**证据**：`grep -rn "memory_source_event" --include=*.py` 在非测试代码中**零命中**；实现初版（纯透传）的 outbox 测试全部 404。

**处置**：在**允许的写入范围内**新增 `LedgerProvenanceRepository`（位于 `internal_api/memory.py`），把 `source_event` 指向计划指定的 ledger，其余方法全部委托给真实仓库。原有保证一条未减：owner + session + event 三者必须匹配、行必须未被清除、**文本从数据库读回而非取自信道调用方**。`memory/` 一行未改。

为此 `migrations/002` 另为 `memory_source_event` 增加 `seq BIGINT AUTO_INCREMENT`（`process_message` 会把 `source_seq`/`source_created_at` 落到 candidate 上，provenance 行必须提供这两个字段；AUTO_INCREMENT 给出严格递增的到达顺序）。

**备选方案（供裁决）**：若计划方更希望由 `memory/` 自身识别 ledger（一处受控的、需放开只读约束的小改动），则**删除该适配器并让 `memory.py` 回归纯透传**即可，其余代码无需变动。
**影响面**：设计 §3「只做透传」的字面表述、计划 §6.6 的数据源假设。**在本项裁决前，P3-3 的通过依赖于该适配器。**

### D2 — 快照 prefetch 不能与身份解析并行
设计 §2 写「与身份解析并行」，但 Service JWT 必须携带 `business_user_id`，而它由身份解析产出，故实际为串行：身份解析 → 快照 prefetch → prompt。设计要求的关键性质（prompt 前完成、不在 `pi.on` 内等 I/O）已满足。

### D3 — 合规扩展在 `message_end` 内 await 网络
交接指令 §2.4 的「pi.on 钩子内禁止 await 网络/DB」若按字面理解，与设计 §4（message_end 内调用合规服务）冲突。本实现按 **plan v2 §6.5** 执行（合规复核在关键路径上属 parity，P95 单列预算），并把该约束理解为针对**记忆/快照注入**路径（v2 §6.6）。若验收方意图更宽，需裁决。

### D4 — 快照以 user 角色内容出现在用户提问之后
Pi 的 `before_agent_start` 在用户 prompt 提交后触发，其返回的 CustomMessage 在模型上下文中以 user 角色出现，且位置**在用户提问之后**。功能正确（内容可读、每轮重新注入），但顺序上快照晚于提问。若希望快照先于提问，需改用 `pi.on("context")` 重排消息（偏离设计 §2 的 `before_agent_start` 指定），建议留待评估。

### 其他实现细节（非偏差，记录备查）
- `internal_api/auth.py` 新增 `resolve_service_session()`，供三个新端点复用账户/归属校验，避免三份重复实现。该文件属 `internal_api/`，在白名单语义内。
- `api/main.py` 仅 +4 行（挂载两个只读数据源到 `app.state`），未改任何既有逻辑。
- `.env.example` 未新增变量（`SMARTCS_TURN_SNAPSHOT_BUDGET`、`SMARTCS_COMPLIANCE_LLM_REVIEW` 均为可选且有默认值；如需登记请示下）。

---

## 7. 遗留与移交

1. **裁决 D1**（记忆 provenance 数据源）——本阶段唯一影响功能语义的决策点。
2. **裁决 D3**（`pi.on` 禁网络约束的适用范围），如需收紧则合规复核需改为「先规则、LLM 复核异步补审」的新设计。
3. **评估 D4**（快照在上下文中的位置）。
4. Phase 4 前置：WRITE Shadow 需在只读工具面之上引入写工具的「只规划不执行」模式，与本阶段的合规/final 缓冲无冲突。

---

## 8. 硬约束合规自查

| 约束 | 状态 |
|---|---|
| 不接 WRITE / 不建 pending_action / 不拆 RAG / compose 不动 | ✅ 全部未触碰 |
| 白名单（新增 3 个 internal_api 文件 + migrations/002 + 对应 tests） | ✅ `git status` 实测 |
| `memory/` provenance 回查语义一行不改 | ⚠️ 见 D1：`memory/` 文件未改，但 provenance **数据源**经适配器切换为计划指定的 ledger，需裁决 |
| `pi.on` 内不 await 网络/DB（快照路径） | ✅ 快照 prefetch + 同步注入；合规路径按 plan §6.5 执行（D3） |
| `text_delta` 永不作为 final 外发；LLM 复核默认关 | ✅ P3-5 断言；`SMARTCS_COMPLIANCE_LLM_REVIEW` 默认 `false`，离线测试不开真实端点 |
| 机器纪律（串行、无长时后台模型任务、无 3GB+ 残留） | ✅ §4.3 |
| pytest ≥ 540；TS 全绿；tsc 干净；不 commit/push | ✅ 559 / 101 / 干净 / HEAD `abf71d2` |
| 测试可重复、命令入报告、全绿或如实列失败 | ✅ §4.1；无失败项 |
| 偏差记报告不静默改文档 | ✅ §6，4 项；计划/设计文档零修改 |

---

**STATUS: completed**

- 设计 §7 验收：**P3-1 ～ P3-9 全部通过**
- TS：**101 passed / 0 failed**（19 文件），typecheck 干净
- Python：**559 passed / 37 skipped**（Phase 2 基线 540 → +19）
- 只读目录零改动；机器纪律全部遵守；无遗留进程
- **1 项需裁决**：D1（记忆 provenance 数据源）
