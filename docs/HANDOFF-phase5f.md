# 交接指令：Phase 5F — F1–F14 全量故障注入矩阵（Phase 5 最终门禁）

> **发件人**：Claude Code（规划与验收方） ｜ **日期**：2026-10-04
> **前置**：5d 轮验收通过（P5-1～P5-12 全集就位，TS 114/114 复现）。**你 5d 报告 F6.1 的边界——TS 决策逻辑（stub）与 Python 权威（真实）未在一条链路里整合跑通——本轮必须补上。**
> **必读**：`pi-replatform-plan-v2.md` §9（F1–F14 定义表）+ `phase5-design.md` §4/§6。

---

## 1. 本轮任务：F1–F14 全部执行，任一失败 = STATUS: failed

**执行形态要求**（区别于此前所有轮次）：
- **真实整合链路**：真 Python 子进程（internal 全套）+ 真 Pi session + 真测试库 + **注入真实故障**（进程 kill -9、socket 销毁、时序窗口）——**不允许用 stub 代替故障注入本身**（决策逻辑已被 5d 覆盖，本轮测的是整合后的真实行为）。
- 每个用例给出：注入方式、观测点（计数/状态/日志）、结果判定。可重复运行。

**逐项口径**（按计划 §9 表）：

| # | 注入 | 必须保证 |
|---|---|---|
| F1 | LLM 调用前杀 Node | 无副作用，receipt 可恢复 |
| F2 | READ 工具 HTTP 中断 | 可安全重试，无副作用 |
| F3 | WRITE 发送前杀 Node | ledger 无 operation，可重新发起 |
| F4 | Python 写成功、响应丢失（socket 销毁） | UNKNOWN → reconcile，禁盲重试 |
| F5 | 写成功、Pi toolResult append 前杀 Node | 从 ledger 收口，无第二条 refund |
| F6 | final 生成后 receipt completed 前杀 | 不重复 WRITE，响应可恢复 |
| F7 | 同 client_request_id 重发 | 不重复 turn/副作用 |
| F8 | 同会话双请求并发 | 单写者、确定顺序 |
| F9 | SSE 断开且写执行中 | abort≠失败，按 ledger 收口 |
| F10 | compaction 后确认退款 | pending 来自 Python 结构化状态 |
| F11 | 工具结果含注入指令 | 仅作数据，工具面不增长 |
| F12 | 进程崩溃后同持久卷恢复 | transcript/业务状态一致 |
| F13 | receipt=processing 恢复（孤儿改判） | 先 reconcile 后行动，单 canonical response |
| F14 | pending 过期后迟到确认 | 拒绝，无写 |

F1-F9/F12/F13 需要**真实进程/传输层注入**；F10/F11/F14 可在进程内注入（状态/文本操纵），但其余链路真实。

## 2. 硬约束

沿用 Phase 5 全部条款。特别地：
- **任一项不过 = STATUS: failed**（不许降级为 blocked 或"部分通过"——那是门禁的本义）。
- 真实写只落测试数据层；机器纪律（kill 后确认无残留进程）；pytest ≥ 576；TS 全绿；不 commit/push。
- 报告**续写**追加"Phase 5F 轮"章节：逐用例的注入方式/观测/结果表、偏差、`STATUS:`、终行完成标记。

## 3. 完成定义

F1–F14 全过 → Phase 5 整体完成（P5 全集 + F 全矩阵）。此后按计划进入 Phase 6（Observability）。
