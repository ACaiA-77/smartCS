# 多轮维修预约路由修复计划与实施记录

## 问题

在同一会话中，用户先咨询 `iPhone 16` 电池维修政策，下一轮说“那我想预约维修”时，系统可能给出“查询订单还是维修设备”的通用澄清，并在 TUI 中显示 `compliance_checker`。这与实际业务语义不符：该轮应继承设备上下文，进入维修工单流程。

## 目标

1. 将明确的维修续接动作确定性路由到 `ticket_handler`。
2. 将普通业务澄清从 `compliance_check` 链路中剥离。
3. 由工单 Agent 补充预约字段，而不是重新询问业务类别。
4. 在 API/TUI 中同时展示业务目标、二级意图与响应模式。

## 实施项

- [x] 在 `agents/intent_router.py` 增加上下文续接判定：上一轮为知识/工单流程、已有设备实体、当前轮含预约/报修/维修动作时，生成 `repair_request`、继承设备实体，并标记 `context_follow_up`。
- [x] 在 `agents/supervisor.py` 增加独立 `clarification` 节点；低置信度业务澄清不再进入 `compliance_check`。
- [x] 在 `agents/ticket_handler.py` 中为缺少地区的维修预约补充城市/地区、设备状态、到店时间等字段，并提醒不要提供密码或验证码。
- [x] 扩展 `/api/chat` 与 TUI 输出：`target`、`secondary`、`response_mode`、`needs_clarification`。
- [x] 新增多轮维修续接、澄清分流、TUI 路由展示的回归测试。

## 验收场景

```text
第 1 轮：我使用的是 iPhone 16，想了解电池维修政策
第 2 轮：那我想预约维修
```

预期：

```text
[target=ticket_handler; secondary=repair_request;
 response_mode=collect_ticket_details; needs_clarification=False]
```

回复应承接 `iPhone 16`，询问地区或设备状态；不得询问“查询订单还是维修设备”，也不得将普通预约显示为合规审查。

## 验证命令

```powershell
Set-Location D:\Workspace_for_Codex\project005_SmartCS\python-impl-intent-fix
python -m pytest -q .\tests\test_contextual_repair_flow.py .\tests\test_clarification_routing.py .\tests\test_tui_route_observability.py
python -m pytest --collect-only -q
```
