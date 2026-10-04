# 交接指令：Phase 5F-2 — F 矩阵补完（Phase 5 最终门禁·续）

> **发件方**：Claude Code（规划与验收方） ｜ **日期**：2026-10-04
> **前置**：5F 轮 blocked 处理认可：**你的 STATUS 口径抗辩成立并已采纳**（failed=跑了且失败，blocked=未跑完——是我的指令表述不精确）。G2 盘点与 G5 清单质量很高，本轮就是按 G5 执行。
> **必读**：本文件 + `PHASE5_REPORT.md` 5F 轮章节（G2 覆盖盘点 + G5 清单）+ `pi-replatform-plan-v2.md` §9。

---

## 0. 执行前提（硬性）

**第一步必须是 `/clear`**——你连续多轮未清上下文（70%+ 一路爬到 78%），这是本轮再次 blocked 的直接原因。清完后从本文件重新开始。上下文再不够就再 blocked，宁缺毋滥。

## 1. 本轮任务（= G5 清单，按序）

1. **F3/F6 注入基础设施**：可编程自杀点（如 env 控制的 `SMARTCS_CRASH_POINT=before_write_send|after_write_success|before_receipt_complete`，测试侧触发 `taskkill /f` 或进程内 `process.abort()`）——这是唯一全新工程。
2. **F4/F5 升级真实注入**：复用 `helpers/tool-proxy.ts`，在 Python 已落库后销毁 socket，再走 `/internal/operation_status` 收口。
3. **F9 真实 SSE 断连**：真实 harness HTTP 服务，写执行中断开客户端连接，验 abort≠失败（账本权威收口）。
4. **F1/F10 补具体时点/动作**（LLM 前 kill 的精确窗口；compaction 后"确认退款"这一具体动作链）。
5. **整合链路**（5d §F6.1 的缺口）：真 Python 子进程 + 真 Pi session + 真实注入在**一条链路**跑通至少 F4/F5/F9 三个场景。
6. **全 14 项按 F 口径各出一条用例**（此前为其他目的写的测试可复用其机制，但要有 F 名义的、注入方式/观测点/判定齐全的用例），可重复运行。

## 2. 硬约束

沿用 Phase 5 全部条款。STATUS 口径修正为：**跑了且有用例失败 = failed；未跑完 = blocked；14 项全过 = completed**。真实写只落测试层；机器纪律（kill 后清点残留进程）；pytest ≥ 576；TS 全绿；不 commit/push；报告续写追加"Phase 5F-2 轮"章节；终行完成标记。

## 3. 完成定义

F1–F14 各有 F 名义用例且全过 + 整合链路三项（F4/F5/F9）真实跑通 → **Phase 5 整体 completed**。
