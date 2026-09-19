# SmartCS Python Impl Handoff

更新时间：2026-09-18（Asia/Shanghai）
项目目录：`D:\Workspace_for_Codex\project005_SmartCS\python-impl`

## 当前状态

- 用户要求暂停后续执行，当前不要开始 Agent Eval、Failure Injection 或下一轮计划。
- Plan 10A 已由 GPT 验收通过。
- Plan 10B `c2c_durable_ticket_recovery_01` 已完成 Revision 2，并由 GPT 验收通过：
  `C2C_REVIEW TASK_ID: c2c_durable_ticket_recovery_01 ITERATION: 2 VERDICT: PASS`。
- 当前工作区保留了此前已有的 dirty WIP；不要 reset、clean、stash 或覆盖无关改动。
- 本次只完成了代码验证和交接准备，没有 commit、push、部署或启动下一计划。

## Plan 10B 已交付内容

- `tickets/service.py` 提供共享 `canonical_ticket_payload_hash`，统一做空白归一化、priority/category 小写、排序 JSON 和 SHA-256。
- `support_tickets` 使用现有 OrderRepository SQLite 数据库；`client_request_id`、`payload_hash`、用户归属和工单内容持久化。
- `ticket_create` 经过 ToolExecutor 写路径，要求 `request_payload_hash`；TicketService 会重新计算权威 hash，不匹配时返回 `invalid_request_payload_hash` 且不插入。
- 同一 `client_request_id` 的同 payload 会 replay；不同 payload 返回 `client_request_conflict`，不泄露旧工单 metadata，跨用户碰撞也一样。
- ExecutionLedger 的 ticket recovery payload 精确包含：`client_request_id`、`user_id`、`request_payload_hash`；不包含 `title` 或 `description`。
- `ExecutionReconciler` 只在 durable `payload_hash` 匹配时恢复成功；冲突会完成为 terminal business conflict（业务 `success=False`），不会重试或返回旧 `ticket_id`；缺少 hash 的历史记录仍是 `manual_required/in_progress`；没有 durable effect 才 conditional release。
- TicketHandler、合规升级和查询均通过 ToolExecutor；已移除 Agent-local `TicketStore`，使用共享 TicketService。

## 验证证据

以下命令均在项目根目录由主 Agent 独立执行：

```text
python -m pytest tests/test_ticket_service.py tests/test_mcp_ticket_tools.py tests/test_execution_recovery.py tests/test_ticket_handler.py tests/test_agent_tool_boundary.py tests/test_orchestrator.py tests/test_safe_tool_api.py -q
74 passed in 95.50s

python -m pytest tests/test_tool_execution.py tests/test_tool_idempotency.py tests/test_tool_approval.py -q
25 passed in 1.78s

python -m pytest tests/test_business_sandbox.py tests/test_refund_service.py tests/test_mcp_refund_tools.py tests/test_refund_handler.py -q
52 passed in 24.15s

python -m pytest -q
222 passed in 234.68s

python -m py_compile tickets/service.py mcp/mcp_server.py mcp/execution_recovery.py agents/ticket_handler.py agents/orchestrator.py tests/test_ticket_service.py tests/test_mcp_ticket_tools.py tests/test_execution_recovery.py
通过

git diff --check
exit 0（只有既有 LF/CRLF 转换提示）
```

安全回归已覆盖：caller hash 不匹配零插入、精确 effect recovery、冲突 stale recovery 的 terminal business conflict、跨用户 request-id 冲突不泄露旧 metadata、recovery payload 不含自由文本。架构扫描结果：`TicketStore`、agents 中 `call_tool`、`WorkingMemory/_wm_context`、`langgraph/StateGraph/MemorySaver` 均为 0 matches。测试期间只有既有 OpenTelemetry exporter 连接 `localhost:4317` 的 warning，不影响退出码。

## 关键文件

- `tickets/service.py`
- `mcp/mcp_server.py`
- `mcp/execution_recovery.py`
- `agents/ticket_handler.py`
- `agents/orchestrator.py`
- `tests/test_ticket_service.py`
- `tests/test_mcp_ticket_tools.py`
- `tests/test_execution_recovery.py`
- `tests/test_agent_tool_boundary.py`

## 接手步骤

1. 先运行 `git status --short` 和 `git diff --cached --stat`，确认暂存区与工作区；不要清理已有 dirty WIP。
2. 需要继续时，先在当前 GPT 对话发送 `[C2C] STATE: REQUEST_NEXT_PLAN`，让 GPT 基于 Plan 10B PASS 给出新的 bounded plan；不要自行跳过计划或直接开始 Agent Eval。
3. 后续每轮按“GPT 计划 → 子 Agent 执行 → 主 Agent 独立验证 → C2C EXECUTED → GPT 明确 PASS”推进。未得到 `C2C_REVIEW ... VERDICT: PASS` 前，不开始下一轮。
4. C2C 执行记录任务 ID 为 `c2c_durable_ticket_recovery_01`；本轮 iteration 2 的执行摘要已写入 C2C records。

## 暂存说明

按用户要求，当前项目中的代码、测试、配置和文档改动已加入 Git 暂存区；OCR 产物目录 `artifacts/` 和异常文件 `nul` 不属于代码，未加入暂存区。当前没有提交 commit。
