# 交接指令：Phase 5c — 恢复机制 + TS live 链路（Phase 5 收官轮之一）

> **发件人**：Claude Code（规划与验收方） ｜ **日期**：2026-10-04
> **前置**：5b 轮验收通过（两段式核心 13/13 复现、全量 572/37）。剩余即本轮范围 + F 矩阵轮。
> **必读**：`phase5-design.md` §4（UNKNOWN/恢复/`/internal/operation_status`）+ `PHASE5_REPORT.md` 5b 轮章节（你在 D1 修正的三处与 D4 未完成清单）。

---

## 1. 本轮任务

1. **`/internal/operation_status`**（Python）：按设计 §4 查 ExecutionLedger/Reconciler 给出权威 `COMPLETED | FAILED | PROVABLY_NOT_EXECUTED | UNKNOWN`。
2. **UNKNOWN → reconcile 消费方**：internal 超时/socket 断 → `mark_operation_state(UNKNOWN)` 禁盲重试；receipt=processing 恢复路径接入 Phase 1 孤儿改判 + open_write_operations 检查（有 write op → 先 reconcile 才允许动作）。
3. **TS 侧 live 链路**：live 工具分支接线（此前 Python 侧已就绪，TS 薄壳仍走 shadow/拦截路径）、operation 状态机、**F5 确定性"业务已完成"恢复路径**（Ledger=COMPLETED 但 transcript 缺 toolResult → Harness 生成确定性结果结束 request，后续轮从 Python 业务状态注入——Phase 3 快照机制天然支持）。
4. **P5-3（幂等重放 refund）/ P5-4（F4 超时→UNKNOWN）/ P5-5（F5 缺 toolResult 恢复）/ P5-8（abort≠失败）** 全部实现并测试。
5. **P5-11 全量确认**（shadow/off 模式全绿回归）+ P5-12（基线）。

完成后 Phase 5 的 P5 用例集（1～12）应全部就位——F1–F14 独立轮随后下发（那是 Phase 5 的最终门禁）。

## 2. 硬约束

沿用 Phase 5 全部条款不变（真实写只落测试层、operation_id 先于发送 durable、UNKNOWN 禁盲重试、abort≠失败、只读目录清单、机器纪律、pytest ≥ 572、不 commit/push、诚实 blocked 分轮、报告续写追加章节、终行完成标记）。

## 3. 交付物

报告追加"Phase 5c 轮"章节：恢复机制证据（UNKNOWN 注入→reconcile 收口链路、F5 恢复行为）、P5-3/4/5/8/11/12 结果表、偏差、`STATUS:`、终行完成标记。
