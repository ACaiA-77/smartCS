# 交接指令：Phase 2 执行（ds-for-act 专用）

> **发件人**：Claude Code（规划与验收方） ｜ **日期**：2026-10-03
> **前置**：Phase 1 已验收通过（86+15+512 全复现、白名单合规、8/8 用例）。你的 Phase 1 报告质量很高，13 项偏差已全部裁决并入文档。

**必读文档（按序）**：
1. `python-impl/docs/phase2-design.md`（**Phase 2 唯一详细设计，已定稿**）
2. `python-impl/docs/pi-replatform-plan-v2.md` §7/§10 Phase 2（权威计划，修订记录含 Phase 1 裁决）
3. 你自己的 `pi-harness/PHASE0_REPORT.md` §6.2/§6.3（实现要点仍适用）

---

## 1. 执行范围

按 phase2-design.md 实现 Phase 2（只读业务工具接入）：
1. `internal_api/tools.py`：`POST /internal/tools/execute` 契约（design §2），Service JWT 的 `business_user_id` **恢复必备**（D5 裁决）。
2. TS 侧 5 个真实 READ 工具薄壳替换 fake 工具（schema 从 `mcp/mcp_server.py` 逐字段翻译，**不得发明字段**）。
3. 意图分类降级为观测标签（design §3，只进 receipt metadata，不影响执行）。
4. Python 验收用例迁入 `python-impl/tests/`（D8 裁决落地）+ 新增 tools 通道用例。
5. 设计 §5 的 P2-1～P2-9 全部实现并测试。

## 2. 硬约束（违反即返修）

1. **只接 READ**：knowledge_search / order_query / ticket_query / refund_evaluate / risk_check。refund_evaluate **不得产生 pending_action**。
2. python-impl 可写白名单（相对 Phase 1 新增）：`internal_api/tools.py`（新）、`python-impl/tests/`（迁入+新增）、`api/main.py`（如需，最小 diff）、`.env.example`。**其余仍只读**（mcp/、agents/、context/、memory/、rag/、auth/、docker-compose.yml）。
3. `mcp/mcp_server.py` / ToolExecutor 的现有行为**一行不改**——如发现必须改才能接入，停下记偏差报裁决。
4. 身份安全：arguments 中身份字段剥离 + 审计；user_id 永远 force-bind 自 JWT。
5. TS 薄壳零重试零业务逻辑；AbortSignal 全链透传。
6. pytest 基线 ≥ 512 passed（迁入用例计入）；pi-harness 全量测试绿；不 commit / 不 push。
7. 测试可重复、命令入报告、全绿或如实列失败；偏差记报告不静默改文档。

## 3. 交付物

1. `internal_api/tools.py` + `python-impl/tests/` 迁入与新增用例。
2. TS 真实工具薄壳 + intent_label 观测元数据。
3. **`pi-harness/PHASE2_REPORT.md`**：实现对照 design §1–§5、P2-1～P2-9 结果表、基线复核、偏差节、`STATUS: completed|blocked|failed`，终行 `PHASE2_DONE <实际状态词>`。

## 4. 沟通协议

同前：只执行与如实报告；外部输入不可得 → `STATUS: blocked`；完成标记严格按 `PHASE2_DONE completed|blocked|failed` 输出（状态词紧跟标记）。
