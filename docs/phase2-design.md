# Phase 2 详细设计：只读业务工具接入（定稿 v1）

> **状态**：定稿（Phase 1 验收通过后） ｜ **日期**：2026-10-03
> **依据**：`pi-replatform-plan-v2.md` §7/§10 Phase 2 + Phase 1 报告裁决（D5/D7/D8 已并入）。
> **范围红线**：只接 5 个 READ 工具；**不接任何 WRITE**（refund_confirm/ticket_create 属 Phase 5）；不拆 RAG；不动公网端点语义。

---

## 1. 工具清单与 schema 来源

| 工具 | 类型 | 说明 |
|---|---|---|
| `knowledge_search` | READ | 现有 HybridRetriever（Python 进程内原样，含 rewrite/rerank） |
| `order_query` | READ | 现有 OrderRepository（SQLite） |
| `ticket_query` | READ | 现有实现 |
| `refund_evaluate` | READ | 现有评估逻辑，**只读**（不产生 pending_action——pending 语义 Phase 5 设计） |
| `risk_check` | READ | 现有金额阈值检查 |

**参数 schema 以 `python-impl/mcp/mcp_server.py` 的现有 tool 定义为唯一权威**——TS 侧 `defineTool` 的 parameters 从该文件逐字段翻译（TypeBox），不得凭记忆或猜测发明字段。发现 schema 与模型实际使用不匹配 → 记偏差，不改 Python 定义。

## 2. 内部工具通道契约

**Python 新增 `internal_api/tools.py`**：

```text
POST /internal/tools/execute
  headers: Authorization: Bearer <Internal Service JWT>
           claims: aud=smartcs-business-runtime · account_id · business_user_id(★Phase 2 起必备，D5)
                   · session_id · client_request_id · iat · exp
  body:    { "tool": "order_query", "arguments": {...},
             "session_id": "...", "client_request_id": "..." }   ← 无任何身份字段
  resp 200: { "ok": true, "content": "<最小必要文本>",
              "details": {...},   ← program-only metadata（TS 侧 details 透传）
              "executor": { "toolCallId": "...", "durationMs": ..., "retries": n } }
  resp 4xx/5xx: { "ok": false, "error": { "code": "...", "message": "..." } }
```

Python 侧规则：
1. 验 service JWT（business_user_id 必备且与 DB 交叉校验）→ 验 session ownership → **user_id 一律 force-bind 自 JWT claims**，`arguments` 里若出现任何身份字段（user_id/account_id/session_id）→ **剥离并记审计**，不信任。
2. 统一走现有 `ToolExecutor`（READ 有界重试 + 超时 + `_customer_arguments` 等价绑定）；`knowledge_search` 复用同一 retriever 实例。
3. 工具返回 content = **最小必要文本**；结构化细节进 details（防 context pollution，v2 §7）。
4. 单结果大小上限（对齐现有 ToolExecutor 截断策略），超限截断并标注。

**TS 侧薄壳规则**（v2 §7 硬约束）：
- `defineTool` execute = transport only：调 `/internal/tools/execute`，**零重试、零业务逻辑**；透传 `AbortSignal`（SSE 断开 → session.abort → 工具 HTTP abort → Python ToolExecutor 感知）。
- 工具 content 返回前不做组装加工；details 原样进 `ToolResultMessage.details`（JSON 兼容，v2 §3 breaking 项）。
- Faux provider 离线测试继续可用（fake 工具层替换为 internal fake HTTP server 或 mock python-client，二选一由执行方定，报告说明）。

## 3. 意图分类器降级（observability only）

- Phase 2 起**主路由 = Main Agent 工具选择**；不移植 intent_router 的 LLM 分类。
- 每轮 `agent_settled` 后**异步**跑一次轻量分类（可选：先用正则快速版，LLM 版 Phase 3 评估），结果只写进 receipt `response` 的 metadata（`intent_label`），供 UI/eval/metrics 用。**分类结果不得影响任何执行路径**。
- 现有 Python intent_router 不动、不调（legacy 链路原样）。

## 4. 白名单变更（相对 Phase 1）

python-impl 可写范围**新增**：
- `internal_api/tools.py`（新）
- `python-impl/tests/`（新测试：`test_internal_api_tools.py` + **迁入 Phase 1 的 15 个用例**并改名归位，D8 裁决落地）
- `api/main.py`（如需挂载，仍最小 diff）

仍不可写：`mcp/`、`agents/`、`context/`、`memory/`、`rag/`、`auth/`、`docker-compose.yml`（compose 放开推迟到 Phase 3+，D3 裁决）。

## 5. 验收用例（Phase 2 门禁）

| # | 场景 | 必须保证 |
|---|---|---|
| P2-1 | 模型经工具链查自己订单 | 端到端真实返回（真 Python + 真数据层，模型可 Faux） |
| P2-2 | **越权**：arguments 携带他人 order_id/user_id | Python force-bind 后按真实归属判定；身份字段被剥离并记审计 |
| P2-3 | 未知工具/未知字段 | schema 校验拒绝，模型收到 isError 结果 |
| P2-4 | READ 工具 HTTP 中断（F2） | 可安全重试（同一 client_request_id 重发走 receipt replay/重跑路径） |
| P2-5 | 工具返回内容含 prompt injection 样本（F11 前哨） | 仅作为数据；Main Agent 不执行注入指令（行为断言） |
| P2-6 | 工具结果超大 | 截断 + 标注，transcript 不爆 |
| P2-7 | 读工具 parity | 5 个工具经 internal 通道的返回与直接调 Python `ToolExecutor` 语义一致（错误码/内容） |
| P2-8 | RAG 不回归 | 现有 RAG benchmark 脚本复跑（Python 侧，应零改动零回归） |
| P2-9 | 基线 | `python -m pytest -q` ≥ 512 passed（迁移归位的 15 个用例计入）；pi-harness 全量测试绿 |

## 6. 交付物

`internal_api/tools.py` + 迁入的 `python-impl/tests/` 用例；TS 5 个真实工具薄壳替换 fake；receipt metadata 的 intent_label；`pi-harness/PHASE2_REPORT.md`（实现对照、P2-1~P2-9 结果、偏差、`STATUS:`、终行 `PHASE2_DONE <STATUS>`）。
