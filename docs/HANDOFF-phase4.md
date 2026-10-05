# 交接指令：Phase 4 执行（ds-for-act 专用）

> **发件人**：Claude Code（规划与验收方） ｜ **日期**：2026-10-04
> **前置**：Phase 3 已验收通过（全量独立复现：TS 101/101、Python 19/19、pytest 559/37 串行零崩溃、D1 适配器采纳）。你的 D1 发现纠正了计划的一个错误假设（legacy 记忆仓库读的是 conversation_event 而非 ledger）——这正是对拍价值的体现。

**必读文档（按序）**：
1. `python-impl/docs/phase4-design.md`（**Phase 4 唯一详细设计，已定稿**）
2. `python-impl/docs/pi-replatform-plan-v2.md` §6.4（三种授权模式）+ 修订记录

---

## 1. 执行范围

按 phase4-design.md 实现 **WRITE Shadow Mode**：
1. `SMARTCS_WRITE_MODE = off|shadow|live` 开关（off 为默认回归 Phase 3 行为；live 本阶段显式 throw 防误开）
2. shadow 工具三件：`refund_evaluate` 附带占位 pending_action_id、`refund_confirm`、`ticket_create`（schema 与 Phase 5 目标面一致、无 confirmed 参数）
3. `shadow_write_plan` 计划记录（建表落 pi-harness 侧，不进 python-impl/migrations）
4. ≥12 场景双侧对拍：pi 侧（Faux 脚本化）× legacy 侧（**必须复用现有 evals/ 基础设施**，禁止重实现 legacy 决策逻辑）
5. P4-1～P4-7 全部实现并测试，硬门禁（越权=0、两段纪律违反=0）必须全过

## 2. 硬约束（违反即返修）

1. **零真实 WRITE**：shadow 拦截路径不得发出任何 internal HTTP 写调用（P4-2 有网络层断言）；不写 python-impl 任何数据文件。
2. **python-impl 预期零改动**——发现必须改就停下记偏差报裁决。
3. 机器纪律沿用（串行测试、无长后台模型任务、结束无 3GB+ 残留）。
4. pytest 559/37 必须实测不变；TS 全绿 + tsc 干净；不 commit / 不 push。
5. 对拍场景必须含"评估→确认"两轮序列与越权场景；不一致 diff 逐条列原因，不许只给总数。
6. 偏差记报告；完成必须输出终行标记（状态词紧跟）；报告不许留悬空占位。

## 3. 交付物

1. `pi-harness/`：开关 + shadow 工具 + 计划存储 + 场景 fixtures + 双侧对拍 runner。
2. **`pi-harness/PHASE4_REPORT.md`**：实现对照 design §1–§6、P4-1～P4-7 结果、**对拍指标表（含硬门禁结论）+ 不一致 diff 清单**、基线实测、偏差、`STATUS:`、终行完成标记。

## 4. 沟通协议

同前。特别提醒：本阶段结论直接决定 Phase 5（真实 WRITE）是否放行——**对拍数据的诚实性高于一切**，不达标就如实报 failed/blocked，禁止为了过关放宽指标。
