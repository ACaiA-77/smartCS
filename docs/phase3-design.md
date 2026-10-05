# Phase 3 详细设计：Context / Memory / Compliance（定稿 v1）

> **状态**：定稿（Phase 2 验收后下发） ｜ **日期**：2026-10-03
> **依据**：`pi-replatform-plan-v2.md` §6.5/§6.6/§6.7/§10 Phase 3 + Phase 0/2 实测结论（A2 replacement 已验证、D11 buildSessionProjection、工具通道已就绪）。
> **范围红线**：不接 WRITE、不建 pending_action（其注入字段本期可为空实现）、不拆 RAG、compose 不动。

---

## 1. 组件与职责

| 组件 | 位置 | 职责 |
|---|---|---|
| Turn Snapshot 服务 | Python `internal_api/context.py` | 一次调用返回本轮业务上下文快照（记忆摘要 + 结构化状态 + 受保护字段） |
| Memory Outbox | TS dispatcher + Python `internal_api/memory.py` | 崩溃安全的记忆写入（取代 fire-and-forget） |
| Compliance 服务 | Python `internal_api/compliance.py` | 规则掩码（必走）+ LLM 复核（可配），返回 verdict/replacement |
| 合规注入扩展 | TS `extensions/compliance.ts` | `message_end` replacement（A2 已验证路径） |
| RunOutputBuffer | TS `streaming/` | final 缓冲；`agent_settled` 后才发 |
| Pi compaction | TS 配置 | 一期用 SDK 默认策略；自定义策略留待评估 |

## 2. Turn Snapshot 契约

```text
POST /internal/context/turn-snapshot
  headers: Internal Service JWT（business_user_id 必备）
  body:    { "session_id": "...", "client_request_id": "..." }
  resp:    { "blocks": [ { "kind": "user_profile|memory_highlights|protected_fields|entity_notes",
               "title": "...", "content": "...", "priority": n } ],
             "tokenBudget": <chars 上限> }
```

- Python 侧复用现有 `memory/` 读取路径与 `context/` 的 block 组装**子集**（只读，不调用现有 invoke_agent）；**不新增 LLM 调用**（快照确定性）。
- TS：`session.prompt()` 前 **prefetch**（与身份解析并行）；扩展在 `before_agent_start` 轻量注入已备好的 blocks（**禁止在 pi.on 里 await 网络**，v2 §6.6 红线）。
- 快照内容 **`protected_fields` 必须来自 Python 权威**，禁止模型从 transcript 历史自行引用（F10 语义：compaction 吃掉历史后事实仍可注入）。

## 3. Durable Memory Outbox

```text
receipt 增列（migrations/002_phase3_memory_outbox.sql）:
  memory_enqueue_status ENUM('pending','done','failed') NOT NULL DEFAULT 'pending'
  memory_attempts TINYINT NOT NULL DEFAULT 0
```

- 写入路径：receipt `completed` 时置 `pending`（Phase 1 已有 `memory_source_event`，本期接通消费）。
- TS dispatcher：每 N 秒扫描 `completed & memory_enqueue_status='pending'`（有界批量）→ `POST /internal/memory/enqueue`（带 source_event_id）→ 成功置 `done`；失败 `memory_attempts+1`，超过阈值置 `failed` 并记审计（**不重放模型/工具**）。
- Python `internal_api/memory.py`：透传 `UserMemoryService.process_message`——**保留其 provenance 回查**（owner/session/event/content 一致才抽取；assistant/tool 文本永不进入）。WAIT_CONFIRM 类正常轮次同样入队（v2 §6.6）。
- 验收基线：`agent_settled` 后 kill -9，重启后 dispatcher 仍能把该轮记忆补投（入队不丢）。

## 4. Compliance 与 final 输出（v2 §6.5 全量落地）

```text
message_end(candidate: 正常 stopReason 且无 toolCall)   ← eligible_final_candidate，latest wins
  → TS 扩展调用 POST /internal/compliance/review {text, session_id, intent_label}
      resp: { "verdict": "pass|sanitize|fail", "replacement": "...|null",
              "rulesHit": [...], "llmReviewed": bool }
  → pass: 原消息 ｜ sanitize: replacement ｜ fail: 确定性安全兜底文案
  → replacement 写回 message（A2 路径：用户所见 == transcript 所存）
  → RunOutputBuffer 暂存
agent_settled → 取 latest candidate → SSE final → done；无合法 candidate → 确定性兜底
```

- **规则掩码必走**（Python 现有 PII/违禁词规则子集，确定性）；**LLM 复核由 env 开关**（`SMARTCS_COMPLIANCE_LLM_REVIEW=true|false`，默认 false 起步——开启后走真实端点，离线测试关闭）。
- 实时 SSE 仍只发确定性 status（Phase 1/2 已实现，维持）；**`text_delta` 永不作为 final 直发**（已有红线，加断言测试）。
- 复核延迟占关键路径属 parity（现状 Python 亦同步合规），P95 预算本期只记录不设门禁。
- Python 合规规则**只读复用**现有 `agents/compliance_checker.py` 的规则表（如需抽公共函数，允许新增 `compliance_rules.py` 并让两者引用——记偏差报备）。

## 5. Pi compaction 接入

- 一期：默认 compaction 开启 + `compaction.modelOverrides`（按 .env 模型）设置 reserveTokens/keepRecentTokens 合理默认值。
- 每轮注入的 snapshot blocks **不受 compaction 影响**（每轮 before_agent_start 重新注入）——这是 F10 的机制保证。
- 记录 `compaction_start/end` 事件到 receipt metadata（观测用）。

## 6. 白名单变更（相对 Phase 2）

python-impl 可写**新增**：`internal_api/{context,memory,compliance}.py`、`migrations/002_*.sql`、`compliance_rules.py`（如需抽取）、对应 `tests/test_internal_api_*.py`。仍不可写：`agents/`、`context/`、`memory/`、`mcp/`、`rag/`、`auth/`、`docker-compose.yml`。

## 7. 验收用例（Phase 3 门禁）

| # | 场景 | 必须保证 |
|---|---|---|
| P3-1 | 快照注入 | transcript 含 blocks 内容；且 **compaction 后**（人为触发压缩）下一轮 blocks 仍完整注入（F10 变体） |
| P3-2 | 记忆不串用户 | A 用户快照/抽取绝不出现 B 用户数据（跨用户注入与 outbox 两侧都测） |
| P3-3 | outbox 崩溃安全 | settled 后 kill -9 → 重启 → 记忆补投恰好一次；无重复 candidate 行 |
| P3-4 | PII 先流出后替换 = 不可能 | 规则命中 PII 的 final：SSE 通道只见掩码后文本；transcript 存的也是掩码后文本 |
| P3-5 | text_delta 永不外发 | 断言 SSE 帧序列无 content/text 类 final 增量 |
| P3-6 | sanitize 状态一致 | 用户收到 == transcript 所存 == RunOutputBuffer 所存 |
| P3-7 | fail 兜底 | 合规 fail → 确定性安全文案 + receipt 照常 completed |
| P3-8 | protected_fields 权威 | 模型试图从历史引用已保护字段时，answer 中的该字段值仍以 Python 快照为准（行为断言） |
| P3-9 | 基线 | pytest ≥ 540 passed（新增用例计入）；pi-harness 全量绿；tsc 干净 |

## 8. 交付物

`internal_api/{context,memory,compliance}.py` + `migrations/002` + 用例；TS prefetch/dispatcher/compliance 扩展/RunOutputBuffer；`pi-harness/PHASE3_REPORT.md`（对照表、P3-1～P3-9 结果、偏差、`STATUS:`、终行 `PHASE3_DONE <实际状态词>`）。
