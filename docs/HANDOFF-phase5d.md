# 交接指令：Phase 5d — TS live 接线（Phase 5 用例集收官轮）

> **发件人**：Claude Code（规划与验收方） ｜ **日期**：2026-10-04
> **前置**：5c 轮验收通过（恢复权威 4/4 + 全量 576/37 复现）。**你的 E3 关键提示准确：Python 权威已就位但 harness 未调用——本轮就是把这条线接通。**
> **必读**：`phase5-design.md` §4/§6 + `PHASE5_REPORT.md` 5c 轮章节。

---

## 1. 本轮任务

1. **TS live 工具分支**：live 模式下 `refund_confirm`/`ticket_create` 薄壳真实调用 Python live 通道（此前走 shadow 拦截）；shadow/off 行为零回归。
2. **TS operation 状态机**：接收 Python 返回的执行结果与 `operationId`，驱动 receipt `open_write_operations` 的 `PREPARED→COMPLETED|FAILED|UNKNOWN` 流转；UNKNOWN 一律走 `/internal/operation_status` reconcile，**禁盲重试**。
3. **F5 确定性恢复路径**：ledger=COMPLETED 但 transcript 缺 toolResult（注入该状态）→ Harness 生成确定性"业务已完成"结果结束该 request，无重放、无第二条 refund。
4. **P5-8（abort≠失败）**：SSE 断开时写执行中 → `session.abort()` 后请求结果按 ledger 权威收口（不按 abort 语义标失败）。
5. **P5-11 全量回归**：shadow 与 off 模式全绿重跑确认（5a 9 项 + Phase 4 套件 + Phase 0-3 套件）。
6. **P5-12 基线**收口。

完成后 P5-1～P5-12 全集就位（F1–F14 独立轮随后，那是 Phase 5 最终门禁）。

## 2. 硬约束

沿用 Phase 5 全部条款（真实写只落测试层、operation_id 先于发送 durable、UNKNOWN 禁盲重试、abort≠失败、只读目录、机器纪律、pytest ≥ 576、TS 全绿 + tsc、不 commit/push、诚实 blocked、报告续写、终行完成标记）。

## 3. 交付物

报告追加"Phase 5d 轮"章节：TS 接线证据（端到端 UNKNOWN→reconcile→收口、F5 恢复、P5-8 断连行为）、P5-11 全量回归结果、基线、偏差、`STATUS:`、终行完成标记。
