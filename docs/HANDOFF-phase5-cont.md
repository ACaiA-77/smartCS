# 交接指令：Phase 5 续轮（返修 + 完成）

> **发件人**：Claude Code（规划与验收方） ｜ **日期**：2026-10-04
> **前置**：Phase 5 第一轮 STATUS: blocked（上下文耗尽，诚实停机，验收方认可该处理）。**P5-9 失败根因已由验收方定位**，见 §1。
> **必读**：本文件 + `phase5-design.md` + 你上轮的 `PHASE5_REPORT.md`（已完成部分不重做）。

---

## 1. P5-9 根因（验收方已诊断，请验证后修复）

**证据**：失败用例的 captured log：
```
internal live write stripped identity fields: tool=ticket_create fields=client_request_id,user_id
```

**因果链**：Phase 2 的身份剥离规则把 `client_request_id` 从 arguments 剥除 → `ticket_create` 缺失其幂等契约必需的 `client_request_id`/`request_payload_hash` → `TicketService` 重算权威 hash 不匹配 → 返回 `success=false` 失败**字典**（不抛异常）→ 未插入 → 计数为 0；而通道把"调用未抛错"误判为 `executed=true`。

**修复方向（遵循既有信任模型，不新发明）**：
1. 剥离规则保持不变（模型提供的 `client_request_id`/`request_payload_hash` 属信任敏感字段，**该剥**——防止模型操纵幂等键）。
2. 但 live 写路径随后**服务端权威注入**两字段：
   - `client_request_id`：取自请求上下文（service JWT / body 的权威值），与 user_id force-bind 同一模式；
   - `request_payload_hash`：服务端用 `tickets/service.py` 的 `canonical_ticket_payload_hash`（**只读 import，不改 tickets/**）对最终注入后的参数计算。
3. `executed` 标志语义修正：必须反映 ToolExecutor/业务结果（handler 返回 `success=false` 字典 → `executed=false` 并透出 `reason_code`），不是"调用未抛错"。
4. 先写一个最小复现测试钉住该根因，再修复。

## 2. 本轮任务（按序）

1. **验证并修复 P5-9**（§1）→ 5a 用例 7/7。
2. `migrations/003` 幂等复跑验证（照 001/002 连跑 3 次）。
3. **5a 验收**：完成后在报告标注"5a ready"，**继续 5b**（pending_action 消费、refund 两段式 live、UNKNOWN/reconcile、`/internal/operation_status`、P5-1~P5-8/P5-11/P5-12）——上下文不够就再次诚实 blocked，分轮推进。
4. **F1–F14 不在本轮**（你上轮建议正确，采纳）：待 5b 完成后单独一轮执行。

## 3. 硬约束（沿用 Phase 5 交接指令全部条款）

重点重申：真实写只落测试数据层；operation_id 先于发送 durable；confirmed 非模型参数；agents/mcp/memory/context/rag/auth 只读（`tickets/` 也只读——hash 函数 import 使用）；不 commit/push；机器纪律；终行完成标记（状态词紧跟）。

## 4. 交付物

更新 `pi-harness/PHASE5_REPORT.md`（**续写而非覆盖**：保留第一轮 blocked 记录，追加续轮章节——根因验证、修复、5a 结果、5b 进度）；终行 `PHASE5_DONE <实际状态词>`。
