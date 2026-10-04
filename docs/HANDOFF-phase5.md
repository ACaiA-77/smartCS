# 交接指令：Phase 5 执行（ds-for-act 专用）— 真实 WRITE 启用

> **发件人**：Claude Code（规划与验收方） ｜ **日期**：2026-10-04
> **前置**：Phase 4+4B 验收通过，**用户已明确放行真实 WRITE**。这是整个迁移的核心阶段。

**必读（按序）**：
1. `python-impl/docs/phase5-design.md`（**唯一详细设计，已定稿**）
2. `pi-replatform-plan-v2.md` §6.3/§6.4/§10 Phase 5 + 修订记录（含 Phase 4 对拍发现：legacy 确认即落库无 pending 中间态）
3. 你自己的 PHASE4/4B 报告（shadow 机制与门禁数据是本期的地基）

---

## 1. 执行范围

按 phase5-design.md 实现，**分两步独立验收**：
- **Phase 5a**：`ticket_create` live（单轮授权）全链路
- **Phase 5b**：`refund_confirm` live（两段式 + pending_action + 完整恢复机制）

含：`migrations/003`（pending_action）、`WriteAuthorizationService`（规则从 agents/ **复制**不移动）、operation_id-before-send、UNKNOWN/reconcile、`/internal/operation_status`、F5 transcript 修复路径、P5-1～P5-12 + **F1–F14 全量故障注入**。

## 2. 硬约束（违反即返修，本阶段最严）

1. **真实写只落测试数据层**（SQLite 测试库 + smartcs_phase1_test）；绝不触碰生产数据文件。
2. **operation_id 在发 WRITE 之前 durable**（receipt + pending_action 双写）——违反即返修且不可辩护。
3. `confirmed` 永不出现在模型参数；授权计算只依赖 DB 权威 + service JWT claims；raw user message 从 **memory_source_event 回读**，不信信道传文。
4. **agents/、mcp/、memory/、context/、rag/、auth/ 仍只读**。ToolExecutor 确认注入机制若不可行 → 停下报偏差（白名单见设计 §5）。
5. UNKNOWN 禁 blind retry；abort ≠ 业务失败；恢复一律 reconcile 优先。
6. 5a 未验收通过（由我判定）不得开 5b；F1–F14 任何一项不过 = STATUS: failed（不许降级表述）。
7. 机器纪律沿用；pytest ≥ 559；TS 全绿；不 commit / 不 push；偏差照记；终行完成标记（状态词紧跟）。

## 3. 交付物

设计 §7 全列 + **`pi-harness/PHASE5_REPORT.md`**（5a/5b 分步记录、P5-1～P5-12、**F1–F14 全矩阵逐项结果**、偏差、`STATUS:`、终行完成标记）。

## 4. 沟通协议

本阶段报告的读者还包括最终用户——**每个安全关键断言给出可独立复跑的命令**。遇阻即 blocked，不伪造。
