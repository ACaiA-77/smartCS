# 交接指令：Phase 5b — refund 两段式 live + 恢复机制

> **发件人**：Claude Code（规划与验收方） ｜ **日期**：2026-10-04
> **前置**：Phase 5a 已验收通过（9/9 复现、全量 568/37、根因回归确认伪造幂等字段不落库）。你的根因验证与修复执行到位。
> **必读**：本文件 + `phase5-design.md` §3/§4/§6（5b 的完整设计）+ `PHASE5_REPORT.md` 续轮章节（你已写但未测试的 5b 方法清单见 C4）。

---

## 1. 本轮任务：完成 Phase 5b

1. **测试并接线已写未测的 `write_authorization.py` 方法**：`create_pending_action` / `load_pending_action` / `expire_pending_action` / `consume_pending_action` / `reserve_operation` 的 pending 分支——**先写测试钉住预期行为再接线**（方法若有 bug 照修，语义以设计 §3.1 为准）。
2. **refund_evaluate live 路径**：live 模式下经内部通道创建真实 `pending_action`（MySQL 权威，含 expires_at TTL 30 分钟）；shadow/off 模式行为不变（Phase 4/5a 已固定的行为零回归）。
3. **refund_confirm live 全链**（设计 §3.1 授权链）：load pending → 验 owner/session/未过期 → 账本回读 raw message 确定性匹配 → operation_id 先于发送 durable → ToolExecutor 执行 refund_create（幂等键 = operation_id）。
4. **UNKNOWN/reconcile**：internal 超时/断连 → state=UNKNOWN 禁盲重试；新端点 `/internal/operation_status`（设计 §4）。
5. **TS 侧**：live 工具分支、operation 状态机、F5 的确定性"业务已完成"恢复路径（设计 §4）。
6. **P5-1～P5-8、P5-11（shadow 回归）、P5-12（基线）** 全部实现并测试（用例表见设计 §6）。

## 2. 硬约束（沿用 Phase 5 全部条款，重点重申）

- 真实写只落测试数据层；operation_id 先于发送 durable；confirmed 非模型参数；授权只依赖 DB + service JWT。
- pending_action 是 Python/MySQL 权威——TS/transcript 永不是权威（F10 语义，设计 §5 的快照机制已在 Phase 3 就位）。
- agents/mcp/memory/context/rag/auth/**tickets** 只读；白名单仍限 internal_api/ + migrations/ + tests/ + api/main.py 最小 diff。
- F1–F14 仍留独立轮，本轮不碰（P5 用例中与 F 组重叠的部分按设计 §6 的 P5 口径做，不用全套注入）。
- 机器纪律；pytest ≥ 568；TS 全绿 + tsc 干净；不 commit/push；上下文不够就诚实 blocked 分轮；报告**续写**（追加第三轮章节）；终行完成标记。

## 3. 交付物

报告追加"Phase 5b 轮"章节：5b 方法测试结果、refund 两段全链证据（测试库 refund 计数、pending_action 状态流转、receipt open_write_operations 状态机）、P5-1～P5-12 结果表、偏差、`STATUS:`、终行完成标记。
