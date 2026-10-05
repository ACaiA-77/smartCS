# Phase 1 详细设计：Session / Receipt Foundation（草案 v1）

> **状态**：已定稿（Phase 0 验收通过，2026-10-03）。依赖项已全部裁决，标注 `[P0]` 处为 Phase 0 实测结论。
> **依据**：`pi-replatform-plan-v2.md`（已并入 Phase 0 裁决）+ `../pi-harness/PHASE0_REPORT.md`（实测依据）。本文档细化到接口与 DDL 级，供 ds-for-act 执行 Phase 1 时直接使用。
> **范围红线**：本阶段不接任何业务工具（fake 工具除外）、不写 WRITE、不建 pending_action 表（Phase 5）、不拆 RAG。

---

## 1. 前置依赖（Phase 0 断言 → 已裁决）

| 断言 | Phase 0 结论 | Phase 1 落地方式 |
|---|---|---|
| A1 `create(cwd,dir,{id})` | **PASS**，但 id 目录内不唯一（D4） | session_id 直接作 Pi id；**"先查后建"纪律**（findById 命中 → open 并校验，未命中 → create），mutex 内执行 |
| A7 reopen | **PASS**，但 `open()` 对缺失路径**静默新建空会话** | reopen 前必须校验文件存在 + id 一致，不得依赖抛错 |
| A8 工具禁用 | **PASS（强于计划）**：白名单下内置工具根本不注册 | 直接用显式 `tools` 白名单 |
| A6 `agent_settled` | **PASS**（retry/compaction/连续 prompt 均 1 次） | 完成信号唯一来源 |

---

## 2. MySQL 变更（DDL 草案）

```sql
-- ① harness 版本固定（会话创建时写死，终身不变）
ALTER TABLE conversation_session
  ADD COLUMN harness_version ENUM('legacy','pi') NOT NULL DEFAULT 'legacy';

-- ② 请求级幂等回执
CREATE TABLE agent_run_receipt (
  id                   BIGINT PRIMARY KEY AUTO_INCREMENT,
  session_id           CHAR(36)     NOT NULL,
  client_request_id    VARCHAR(64)  NOT NULL,
  request_hash         CHAR(64)     NOT NULL,          -- sha256(canon(message))
  status               ENUM('processing','completed','failed_recoverable')
                                    NOT NULL DEFAULT 'processing',
  response             JSON         NULL,              -- final answer 结构（completed 时）
  open_write_operations JSON        NULL,              -- Phase 5 启用；Phase 1 恒空数组
  pi_revision_note     VARCHAR(255) NULL,              -- 备注（Pi 文件 transcript 无 revision，留审计位）
  created_at           TIMESTAMP(3) DEFAULT CURRENT_TIMESTAMP(3),
  updated_at           TIMESTAMP(3) DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uq_request (session_id, client_request_id),
  KEY idx_status (status, updated_at)                  -- F13 恢复扫描用
) ENGINE=InnoDB;

-- ③ 用户消息 provenance 最小账本（记忆抽取的 durable source）
CREATE TABLE memory_source_event (
  event_id          CHAR(36)     NOT NULL,
  session_id        CHAR(36)     NOT NULL,
  user_id           BIGINT       NOT NULL,             -- business_user_id
  client_request_id VARCHAR(64)  NOT NULL,
  content           MEDIUMTEXT   NOT NULL,             -- 仅 USER_MESSAGE 原文
  created_at        TIMESTAMP(3) DEFAULT CURRENT_TIMESTAMP(3),
  cleared_at        TIMESTAMP(3) NULL,                 -- DELETE history 时标记
  PRIMARY KEY (event_id),
  KEY idx_session (session_id, created_at),
  KEY idx_cleared (cleared_at)
) ENGINE=InnoDB;

-- ④ 裁决：不建（D4 采用"先查后建"纪律；多实例水平扩展时再评估）
-- CREATE TABLE pi_session_registry (...);
```

迁移脚本位置：`python-impl/migrations/001_phase1_session_foundation.sql`（新目录；现 MySQL schema 归 `platform_db`，脚本只做增量，幂等可重跑）。

---

## 3. Receipt 状态机（Phase 1 范围）

```text
                 ┌──────────────────────────────┐
 new request ──▶ │ processing                   │
                 └──────┬───────────────┬───────┘
        正常完成         │               │ 异常（崩溃/abort 且无 open ops）
                        ▼               ▼
                 ┌────────────┐   ┌───────────────────┐
                 │ completed  │   │ failed_recoverable │──▶ 客户端重发同 request_id
                 └────────────┘   └───────────────────┘      时允许从头重跑
```

**Lookup 决策（按 v2 §5.3 执行）**：
1. ownership 校验（session 归属当前 account）先行，任何 receipt 状态之前。
2. `(session_id, client_request_id)` 唯一键命中：
   - `completed` + 同 hash → replay 存储的 response（不重跑 prompt）
   - `completed` + 异 hash → 409
   - `processing`：本阶段 `open_write_operations` 恒空 → 属于孤儿回执（进程崩溃残留）→ 原子改判 `failed_recoverable` 后按新请求重跑
   - `failed_recoverable` + 同 hash → 重跑
   - `failed_recoverable` + 异 hash → 409

Phase 1 的"改判"必须做成条件 UPDATE（`WHERE status='processing'`），并发下天然 CAS。

---

## 4. SessionRegistry（TS 接口草案）

```ts
export interface SessionLease {
  sessionId: string;
  release(): void;               // 归还；队列中下一个 waiter 接管
}

export interface ActiveSession {
  sessionId: string;
  session: AgentSession;          // Pi 实例（运行时缓存，非 authority）
  lastUsedAt: number;
  inFlight: number;              // 活跃请求数（恒 0 或 1，断言用）
}

export interface SessionRegistry {
  // 同 session 请求严格串行：第二个 acquire 排队（有界队列，超时 30s → 409/429）
  acquire(sessionId: string, timeoutMs?: number): Promise<SessionLease>;
  get(sessionId: string): ActiveSession | undefined;
  // idle 回收：wait idle → dispose() → 移除。不承担保存（durability 在 Pi append）
  scheduleIdleEviction(sessionId: string, afterMs: number): void;
  stats(): { active: number; queued: number; evicted: number };
}
```

- **一期禁用 mid-run steer/followUp**：所有用户输入经 HTTP → registry 排队，`inFlight` 恒 ≤1（F8 断言点）。
- 进程重启后 registry 为空：按需 reopen（`[P0-A7]`）。
- `dispose()` 同步调用；`runtime.dispose()` 如使用必须 await。
- 每会话 SSE `unsubscribe()` 在 `req.on('close')` 与 `finally` 双保险。

---

## 5. 请求处理管道（Phase 1 端到端最小实现）

```text
POST /api/chat | /api/chat/stream        （TS pi-harness，公开端点）
 1. JWT 本地验签（HS256 共享 AUTH_JWT_SECRET）
 2. POST /internal/auth/verify → account 状态 + account_id→business_user_id（单一权威在 Python）
 3. registry.acquire(sessionId)           ← 同会话串行入口
 4. ownership 校验 + receipt lookup（§3 决策）
 5. INSERT memory_source_event（USER_MESSAGE durable，先于任何 LLM 活动）
 6. Pi session: 已活跃则复用；否则 **先查后建**——mutex 内 `findById(sessionId)`：命中 → 校验文件存在且 id 一致后 `open`（防静默新建）；未命中 → `create({id})`。
 7. session.prompt(message)——Phase 1 工具集 = Phase 0 的 2 个 fake 只读工具（或空集）
 8. final（RunOutputBuffer 规则照 v2 §6.5，Phase 1 可先无合规、仅缓冲）
 9. UPDATE receipt SET status='completed', response=... 
10. 响应（JSON 一次性 / SSE status→final→done）
```

**崩溃窗口语义（Phase 1 版）**：步骤 5 之前崩溃 → 无任何持久痕迹，客户端安全重发；5-9 之间崩溃 → receipt 停在 processing → 重发时走 §3 孤儿改判。**没有 WRITE，所以不存在 UNKNOWN 副作用**——这是 Phase 1 敢于"从头重跑"的前提，Phase 5 起该逻辑作废换 v2 §6.3 决策表。

---

## 6. 内部通道契约（本阶段只实现 auth）

**Python 新增 `python-impl/internal_api/auth.py`**（挂载为 FastAPI 子路由，仅内网监听/网络隔离，不复用公网 auth 语义）：

```text
POST /internal/auth/verify
  headers: Authorization: Bearer <Internal Service JWT>
           aud=smartcs-business-runtime（TS 签发，INTERNAL_SERVICE_JWT_SECRET，TTL 60s）
  body:    { "user_jwt": "<公开 JWT 原文>" }
  resp:    { "account_id": ..., "business_user_id": ..., "status": "active|disabled",
             "session_id": ..., "harness_version": "pi" }   ← 顺带做 ownership+版本核对
  错误:    401 service JWT 无效 / 403 user JWT 无效或账户禁用 / 404 session 不属于该账户
```

规则：身份只从 user_jwt 解出，**永不从 body 字段取 user_id**；harness_version 非 pi → 409（走 legacy 端点，防误路由）。

---

## 7. History 投影与 DELETE（harness-aware，Phase 1 最小版）

- TS `GET /internal/history/{session_id}`（service JWT 鉴权）：打开 Pi session 文件 → active branch → 投影 user/assistant（role/content/created_at）→ 稳定 DTO。**不做 compaction 视图**（v2 §6.8 红线）。context edit 的 replacement/omission **直接用 SDK 内建 `buildSessionProjection()`**（Phase 0 D11 实测：projection 看到替换后内容、`getEntries()` 保留原始内容，语义与本设计要求完全一致）。
- Python `GET /api/history/{session_id}`：验 ownership → 按 harness_version 分发：legacy → 旧 projector；pi → 转发 TS internal。
- `DELETE /api/history/{session_id}`（pi 分支）：registry acquire（活跃 run 拒绝 409）→ dispose → 删 Pi session 文件 → `memory_source_event.cleared_at` 打标 → 按现有语义删 platform session。

---

## 8. 其他交付物

- 根 `AGENTS.md` 更新：新增 pi-harness 平级目录说明（职责、`npm ci / npm test / npm run dev` 命令、接口边界、docs 规则）。
- compose 增量：pi-harness 服务 + `SMARTCS_PI_SESSION_DIR` 持久卷（本阶段可先本地开发态，compose 改动随 Phase 1 报告评估）。
- env 新增：`SMARTCS_RUNTIME_CWD` `SMARTCS_PI_SESSION_DIR` `SMARTCS_PI_AGENT_DIR` `PYTHON_INTERNAL_BASE_URL` `INTERNAL_SERVICE_JWT_SECRET`（.env.example 同步）。

## 9. 验收测试映射（Phase 1 故障场景 → 用例）

| 场景 | 用例要点 | 对应 F |
|---|---|---|
| 进程重启 | kill -9 后同 session 续聊，transcript 连续 | F12 |
| 容器重启 | compose 重启 + 持久卷，同上 | F12 |
| 重复 request_id 同 hash | replay 原响应，Pi 无新 entry | F7 |
| 重复 request_id 异 hash | 409 | — |
| 同会话双请求并发 | 第二个排队；`inFlight≤1`；顺序确定 | F8 |
| receipt 孤儿改判 | 人为留 processing 回执 → 重发 → 恰好一次重跑 | F13 前身 |
| idle 回收后续聊 | dispose 后 reopen，历史完整 | — |
| 权限 | 跨账户 session → 403/404；harness_version=legacy → 409 | — |

## 10. 交付物清单（Phase 1 完成定义）

1. `pi-harness/` Phase 1 代码 + 测试（上述用例全绿，可重复运行）。
2. `python-impl/internal_api/auth.py` + 挂载 + pytest（不破坏 367 基线）。
3. `migrations/001_phase1_session_foundation.sql`（幂等）。
4. 根 AGENTS.md 更新。
5. `pi-harness/PHASE1_REPORT.md`：A 项依赖项的落地方式、用例结果、偏差、`STATUS: completed|blocked|failed`、终行 `PHASE1_DONE <STATUS>`。

## 11. Phase 0 实测实现要点（执行必读）

- **扩展注入**：`createAgentSession` 无 `extensionFactories` 参数，经 `DefaultResourceLoader({ extensionFactories: [...] })` 传入；`noExtensions: true` 只关闭文件系统扫描、不关 factories——"关闭 `~/.pi` 发现"与"注入自有扩展"同时成立。
- **durability（D1）**：会话消息 append 返回即落盘；**setup-only entry（含 `appendCustomEntry`）在首个会话消息前不落盘**——Phase 1 任何"turn 开始前持久化"需求一律走 MySQL（receipt / memory_source_event）。
- **`open()` 静默新建**：对不存在的路径不抛错而是新建空会话（新 id）——所有 reopen 路径必须前置校验文件存在 + id 一致。
- **无跨进程锁**：session 文件唯一写者保障 = 本设计的 SessionRegistry（单实例 + mutex + 先查后建），别无他物；同毫秒双 create 会 EEXIST 崩溃、跨毫秒双 create 会 split-brain，均由 mutex + 纪律防住，必须有测试。
- **`message_update`**：同时有 `text_delta` 与 `text_end`（content 已组装完整）——final buffer 优先消费 `text_end`，delta 仅作进度参考。
- **provider 事件**：`before_provider_request` 在 Faux provider 下不可达（不调 `onPayload`）——Phase 1 测试不依赖它，Phase 6 需另设计（D12）。
- **模型配置（D2 裁决）**：以 `python-impl/.env` 为权威（Moonshot/kimi-k2.7-code），compat 四项按 Phase 0 固定值。
