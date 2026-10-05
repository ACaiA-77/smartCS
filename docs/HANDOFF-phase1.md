# 交接指令：Phase 1 执行（ds-for-act 专用）

> **发件人**：Claude Code（规划与验收方）
> **收件人**：ds-for-act（执行方）
> **日期**：2026-10-03
> **前置**：Phase 0 已验收通过（46/46 测试、A1–A8 全 pass、版本冻结 1.0.1）。你的 Phase 0 报告质量很好，D1–D5 已由计划方裁决并并入计划正文。

**必读文档（按序）**：
1. `python-impl/docs/pi-replatform-plan-v2.md`（权威计划，已并入 Phase 0 裁决：§3/§5.2 有修订）
2. `python-impl/docs/phase1-design.md`（**Phase 1 唯一详细设计，已定稿**——DDL、receipt 状态机、SessionRegistry 接口、internal auth 契约、验收用例全在内）
3. `pi-harness/PHASE0_REPORT.md`（你自己产出；§6.2/§6.3 的 delta 是实现要点，已浓缩进 phase1-design §11）

---

## 1. 本次执行范围

**只执行 Phase 1（Session / Receipt Foundation），严格按 phase1-design.md 实现**：

1. **MySQL DDL**：`python-impl/migrations/001_phase1_session_foundation.sql`（幂等可重跑；含 conversation_session.harness_version、agent_run_receipt、memory_source_event；**不建** pi_session_registry——D4 已裁决走"先查后建"纪律）
2. **SessionRegistry + per-session mutex**（含先查后建纪律、`open()` 前置校验、idle dispose/reload；接口见设计 §4）
3. **agent_run_receipt 状态机**（lookup 决策、孤儿回执条件 UPDATE 改判；见设计 §3）
4. **memory_source_event 写入**（USER_MESSAGE durable，先于任何 LLM 活动）
5. **端到端最小 chat 管道**：`POST /api/chat`（JSON）+ `POST /api/chat/stream`（SSE status→final→done），JWT 本地验签 + Python 身份解析
6. **Python `internal_api/auth.py`**：`POST /internal/auth/verify` 契约照设计 §6（身份永不出现在 body；harness_version 非 pi → 409），挂载进现有 FastAPI（内网隔离），**不破坏现有 pytest 基线**
7. **History projector + DELETE**：TS 侧用 `buildSessionProjection()`（D11），Python `/api/history` 按 harness_version 分发
8. **根 AGENTS.md 更新**：登记 pi-harness 平级目录（职责/命令/边界/docs 规则）
9. **工具集维持 Phase 0 的 2 个 fake 只读工具**（不接任何真实业务工具）

## 2. 硬约束（违反即返修）

1. 不接真实 WRITE、不接任何真实业务工具（fake 除外）、不建 pending_action（Phase 5）、不拆 RAG、不动 auth 公网语义。
2. `python-impl/` 内的改动**仅限**：`internal_api/`（新增）、`migrations/`（新增）、`api/main.py` 的子路由挂载（最小 diff）、根 `AGENTS.md`、`.env.example`（新增变量注释）。**其他现有文件一律只读**；`python -m pytest -q` 基线（367 passed / 18 skipped）不得破坏。
3. 不 commit、不 push。
4. MySQL 迁移在本地测试实例执行验证（compose MySQL :3307）；如本机无可用 MySQL，用 SQLite 模拟数据层做逻辑测试并在报告标注（不得伪造 MySQL 验证结论）。
5. 测试必须可重复、命令入报告、全绿或如实列失败；计划/设计与 SDK 实际冲突 → 记入报告"偏差"节，不静默改文档。
6. Phase 1 验收用例（设计 §9 的 8 行表格）逐项实现，**任何一项不过即视为未完成**（这是 Phase 2 的门禁，v2 计划原文要求）。

## 3. 交付物

1. `pi-harness/` Phase 1 代码 + 测试（含 §9 全部用例）。
2. `python-impl/internal_api/` + `migrations/001_*.sql` + 最小挂载 diff + AGENTS.md 更新。
3. **`pi-harness/PHASE1_REPORT.md`**（验收唯一入口）：实现清单 vs 设计 §1–§11 逐项对照、§9 用例结果表（每项：用例名 + 结果 + 关键输出）、pytest 基线复核结果、偏差节、`STATUS: completed|blocked|failed`，终行输出 `PHASE1_DONE <STATUS>`。
4. 完成后终端输出 `PHASE1_DONE <STATUS>` 供监听捕获。

## 4. 沟通协议

- 同 Phase 0：只执行与如实报告；验收、返修、计划修订由我发起。
- 外部输入不可得（如 MySQL）→ `STATUS: blocked` + 说明，不伪造。
- 注意：发给你的提示词文本里若含完成标记字样会被监听误报，最终标记务必按 `PHASE1_DONE completed|blocked|failed` 的实际状态词输出。
