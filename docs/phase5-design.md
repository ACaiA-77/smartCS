# Phase 5 详细设计：WRITE Enable + Recovery（定稿 v1）

> **状态**：定稿（Phase 4+4B 验收通过，用户已放行真实 WRITE） ｜ **日期**：2026-10-04
> **依据**：`pi-replatform-plan-v2.md` §5.4/§6.3/§6.4/§10 Phase 5 + Phase 4B 实测（门禁三轮全零）+ Phase 4 对拍发现（legacy 确认即落库无 pending 中间态）。
> **红线**：所有真实写发生在**测试数据层**（SQLite 测试库 + smartcs_phase1_test MySQL）；授权、幂等、恢复的权威全部在 Python。

---

## 1. 开启顺序（分两步，每步独立验收）

- **Phase 5a — ticket_create（EXPLICIT_SAME_TURN，单轮授权）**：风险低（可撤销的记录型写），先验证完整 live 链路。
- **Phase 5b — refund_confirm（PREPARE_THEN_CONFIRM，两段式 + pending_action）**：在 5a 链路验证后开启。

`SMARTCS_WRITE_MODE=live` 生效范围按工具粒度分步：`ticket_create` 先 live，`refund_confirm` 保持 shadow 直至 5a 验收。

## 2. 数据模型（migrations/003_phase5_write_enable.sql）

```text
pending_action（Python/MySQL 权威，从 Redis session_state 迁出）
  id CHAR(36) PK · session_id · user_id · type ENUM('refund_create')
  payload JSON（order_id/amount/refund_mode 等评估快照）
  status ENUM('pending','consumed','cancelled','expired') DEFAULT 'pending'
  operation_id CHAR(64)          ← 消费时分配，与 receipt/ledger 对齐
  created_at · expires_at（TTL 默认 30 分钟）
  KEY(session_id), KEY(status, expires_at)

receipt.open_write_operations（Phase 1 已建列，本期启用）
  [{operation_id, tool, target_hash, state: PREPARED|SENT|COMPLETED|FAILED|UNKNOWN}]
```

## 3. WriteAuthorizationService（Python，`internal_api/write_authorization.py`）

从 `agents/refund_handler.py`（确认/取消短语）与 `agents/ticket_handler.py`（`_has_explicit_create_consent`）**复制**确定性规则（**agents/ 原文件不动**——legacy 仍用；复制处注明来源与同步义务，legacy 下线时消除双源）。

### 3.1 授权链（refund_confirm）

```text
TS refund_confirm(pending_action_id) 薄壳 → /internal/tools/execute(live)
Python:
  1. service JWT + ownership + session 校验（复用 resolve_service_session）
  2. WriteAuthorizationService:
     load pending_action（MySQL 权威）
     → 验 owner/session 一致
     → 验 status=pending 且未过期（过期→原子置 expired，返回 PENDING_ACTION_EXPIRED）
     → 确定性短语匹配：raw user message 从 memory_source_event 按 (session_id, client_request_id) 回读
       （不信信道传文；回读失败/不匹配 → fail closed）
     → 授权成立：分配 operation_id，durable 写入 pending_action.operation_id + receipt.open_write_operations（先于发送！）
  3. ToolExecutor 执行 refund_create（confirmed=True 由本服务计算注入；幂等键 operation_id）
  4. 结果（含 UNKNOWN）写回 receipt → 返回 TS → toolResult 落 transcript
```

### 3.2 授权链（ticket_create，EXPLICIT_SAME_TURN）

同轮 raw message 回读 → 同轮显式建单意图短语匹配（复制规则）→ 通过 → operation_id 流程同上；不通过 → 业务拒绝结果给模型（fail closed，200+ok）。

### 3.3 通用铁律

- `confirmed` **永远不是模型参数**（schema 无此字段，Phase 4 已定型）
- 授权计算在 Python，输入只有 DB 权威数据 + service JWT claims
- **operation_id 必须在发 WRITE 前 durable**（§6.3 铁律，违反即 F4/F5 不可恢复）

## 4. Unknown Outcome 与恢复

- internal 调用超时/socket 断 → **UNKNOWN**：receipt 的 operation state 置 UNKNOWN，**禁止 blind retry**
- 新端点 `/internal/operation_status`（Python）：查 ExecutionLedger/Reconciler 给出权威 `COMPLETED|FAILED|PROVABLY_NOT_EXECUTED|UNKNOWN`
- 恢复决策表按 v2 §6.3 执行；receipt=processing 的恢复路径走 Phase 1 孤儿改判 + open_write_operations 检查（有 write op → 先 reconcile，禁盲跑）
- **transcript 缺 toolResult 修复（F5）**：Ledger=COMPLETED 但 transcript 无 toolResult → Harness 生成确定性「业务已完成」结果结束该 request，后续轮从 Python 业务状态注入事实（Phase 3 快照机制天然支持）；不做 transcript 重放

## 5. 白名单（相对 Phase 4）

python-impl 可写**新增**：`internal_api/write_authorization.py`、`internal_api/operation_status.py`、`internal_api/tools.py`（live 分支）、`internal_api/__init__.py`、`migrations/003_*.sql`、对应 `tests/test_internal_api_*.py`。**agents/、mcp/、memory/、context/、rag/、auth/ 仍只读**。若 ToolExecutor 确认机制无法从内部通道注入（需读 `mcp/tool_execution.py` 确认），**停下报偏差**，不得自行改 mcp/。

## 6. 验收用例（门禁）

| # | 场景 | 必须保证 |
|---|---|---|
| P5-1 | refund 两段全链（真实写） | evaluate 建真实 pending_action → 确认轮 confirm → **测试库恰一条 refund**；transcript/ledger/receipt 三处一致 |
| P5-2 | 未确认 | confirm 永不发生；pending_action 留 pending |
| P5-3 | **幂等重放** | 同 client_request_id 重发 → replay 原响应，refund 计数不变（F7） |
| P5-4 | **F4 超时→UNKNOWN** | 注入 socket 断 → state=UNKNOWN → reconcile 后按权威结果收口，**无第二条 refund** |
| P5-5 | **F5 transcript 缺 toolResult** | 写成功后杀进程于 append 前 → 恢复不走盲跑、不重复执行 |
| P5-6 | F14 过期确认 | expires_at 过后 confirm → PENDING_ACTION_EXPIRED，无写 |
| P5-7 | 越权 confirm | 非本人 pending / 跨 session → fail closed |
| P5-8 | abort≠失败 | SSE 断开时写执行中 → 结果按 ledger 权威，不按 abort 语义（F9） |
| P5-9 | ticket live | same-turn 授权成立恰一单；无意图轮建单 = 0 |
| P5-10 | ticket 幂等 | 重放不重复建单 |
| P5-11 | shadow 回归 | WRITE_MODE=shadow 仍全绿（机制未被 live 破坏） |
| P5-12 | 基线 | pytest ≥ 559；TS 全绿；tsc 干净；python-impl 白名单合规 |
| F1–F14 | 故障注入矩阵 | **全量执行**（此前未覆盖的 F 组用例全部落地），逐项出结果 |

## 7. 交付物

`internal_api/{write_authorization,operation_status}.py` + tools live 分支 + `migrations/003` + 用例；TS live 工具分支 + operation 状态机 + 恢复路径；**`pi-harness/PHASE5_REPORT.md`**（含 5a/5b 分步记录、P5-1～P5-12、F1-F14 矩阵结果、偏差、`STATUS:`、终行完成标记）。
