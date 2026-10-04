# 交接指令：Phase 7 执行（ds-for-act 专用）— Cohort 灰度

> **发件方**：Claude Code（规划与验收方） ｜ **日期**：2026-10-04
> **前置**：Phase 6 + 6b 验收通过。用户已确认直接进入 Phase 7。
> **必读**：`python-impl/docs/phase7-design.md`（唯一详细设计）+ `pi-replatform-plan-v2.md` §10 Phase 7/§13。

---

## 1. 执行范围

按 phase7-design.md 实现：
1. `SMARTCS_PI_ROLLOUT_PERCENT` 灰度开关 + **确定性分桶**（SHA-256 稳定 hash，禁 Python hash()）
2. 会话创建写死 harness_version（`platform_db/sessions.py` create 最小 diff——本阶段授权写入，仅此一处）
3. 统一入口 chat 分发（pi 会话转发 TS、legacy 走原链路、**pi 不可达宁 503 不降级**）
4. P7-1～P7-7 全部实现并测试（P7-3 需跨进程重启验证分桶恒定）

## 2. 硬约束

1. **harness_version 创建即固定、终身不变、绝不按 intent 切**——任何后续改动违反此条即返修。
2. python-impl 白名单本阶段：`platform_db/sessions.py`（仅 create 路径）、`internal_api/harness_client.py`、`api/main.py`、`.env.example`。**`agents/` 及其余业务目录仍零改动**（legacy orchestrator 一行不动）。
3. 机器纪律 + **测试库窗口纪律**（本轮声明占用窗口再跑套件）沿用；pytest ≥ 585；TS 全绿；不 commit/push；诚实口径；报告新建 `PHASE7_REPORT.md`；终行完成标记。

## 3. 交付物

设计 §6 全列。完成后进入终局验收准备（Phase 8 为可选演进，另行评估）。
