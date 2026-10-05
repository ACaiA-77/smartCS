# Phase 1 报告：Session / Receipt Foundation

> **执行方**：ds-for-act（本终端） ｜ **日期**：2026-10-03
> **依据**：`../python-impl/docs/HANDOFF-phase1.md` + `../python-impl/docs/phase1-design.md`（已定稿）+ `../python-impl/docs/pi-replatform-plan-v2.md`
> **范围**：只执行 Phase 1。未接真实 WRITE、未接真实业务工具、未建 pending_action、未拆 RAG、未动公网 auth 语义；未 commit / 未 push。
> **STATUS: completed**

---

## 0. 结论速览

| 项 | 结论 |
|---|---|
| 交付物 | DDL 迁移、`internal_api/`、`api/main.py` 最小挂载 + history 分发、根 AGENTS.md、.env.example、TS Phase 1 全套 |
| 设计 §9 验收用例 | **8/8 通过**（其中「容器重启」以等价路径覆盖，见 §6-D3） |
| TS 测试 | **86 passed / 0 failed**（17 文件），`tsc --noEmit` 干净 |
| Python Phase 1 测试 | **15 passed**（internal_api 11 + history 分发 4） |
| pytest 基线 | **512 passed / 37 skipped，改动前后完全一致**（见 §0.1） |
| MySQL | **真实实例**（compose :3307）；迁移脚本连跑 3 次幂等；测试用独立库 `smartcs_phase1_test` |
| python-impl 改动范围 | 严格限于白名单：`.env.example`、`api/main.py`(+41/−2)、`internal_api/`(新)、`migrations/`(新) |
| 偏差 | 13 项，记于 §6；**未静默修改任何计划/设计文档** |

### 0.1 ⚠️ 需要验收方注意：文档中的 pytest 基线数字已过期

交接指令 §2 与设计文档均写「基线 367 passed / 18 skipped」。**实测基线是 `512 passed / 37 skipped`**（改动前、改动后各跑一次，数字完全一致，见 §4.2）。测试套件在记录该数字之后已经增长（context engineering、jieba RAG 等提交）。本次以**实测的 512/37** 作为不破坏基线，特此标注，未按过期数字判断。

---

## 1. 实现清单 vs 设计 §1–§11

| 设计节 | 要求 | 落地 | 证据 |
|---|---|---|---|
| §1 前置依赖 | A1/A7/A8/A6 的落地方式 | **先查后建**（`findById` → 校验文件存在 + id 一致 → `open`；未命中 → `create`），内置工具走显式 `tools` 白名单，`agent_settled` 作唯一完成信号 | `src/session/pi-session.ts`；`tests/phase1-registry.test.ts`「两 acquire 恰好一个文件」 |
| §2 DDL | harness_version、agent_run_receipt、memory_source_event；**不建** pi_session_registry | `migrations/001_phase1_session_foundation.sql`，幂等（information_schema 守卫 + prepared statement） | 连跑 3 次 OK；列/索引结构见 §4.3 |
| §3 Receipt 状态机 | lookup 决策表 + 孤儿条件 UPDATE 改判 | `ReceiptStore.beginRequest` 完整实现 5 条决策分支；改判用 `UPDATE ... WHERE status='processing'` CAS | `tests/phase1-receipts.test.ts`（7 用例） |
| §4 SessionRegistry | acquire/get/scheduleIdleEviction/stats；inFlight ≤ 1；队列有界 + 超时 | 全部实现，另加 `tryAcquire`（DELETE 不排队）、`withExclusive`、`disposeNow`、`ensureSession`、`originOf`、`shutdown` | `tests/phase1-registry.test.ts`（9 用例） |
| §5 请求管道 | 10 步端到端 | `src/server/chat-pipeline.ts` 逐步实现并注释编号 | `tests/phase1-e2e.test.ts` |
| §6 内部通道 | `POST /internal/auth/verify` 契约 | `internal_api/auth.py` + `service_jwt.py`；身份只从 user_jwt 解出；非 pi → 409 | `tests/python/test_internal_api_auth.py`（11 用例） |
| §7 History 投影与 DELETE | TS 用 `buildSessionProjection()`；Python 按 harness_version 分发 | `src/history/projector.ts`；`api/main.py` 的 `get_history`/`clear_history` pi 分支转发 TS internal | `tests/python/test_history_dispatch.py`（4 用例）+ E2E history/DELETE 用例 |
| §8 其他交付物 | AGENTS.md、compose 增量、env 新增 | AGENTS.md ✅；env ✅（含 1 项新增，见 §6-D7）；**compose 增量未做**（不在写白名单，见 §6-D3） | 见 §2 |
| §9 验收用例 | 8 行表格逐项 | 见 §3 | — |
| §10/§11 | 交付物清单 / Phase 0 要点 | 全部遵守（扩展经 resourceLoader 注入、durability 走 MySQL、open() 前置校验、无跨进程锁靠 registry、final 优先 text_end、不依赖 provider 事件、模型配置以 .env 为准） | — |

### 1.1 目录结构（新增）

```text
python-impl/
├─ internal_api/                        ← 新
│  ├─ __init__.py                       (router 导出)
│  ├─ service_jwt.py                    (Internal Service JWT 校验)
│  ├─ auth.py                           (POST /internal/auth/verify)
│  └─ harness_client.py                 (runtime → harness 签名调用)
└─ migrations/
   └─ 001_phase1_session_foundation.sql

pi-harness/src/
├─ db/{mysql,receipts,memory-source}.ts
├─ session/{registry,pi-session}.ts
├─ business/{jwt-hs256,python-client}.ts
├─ history/projector.ts
├─ server/{app,chat-pipeline,user-auth,http-error,main}.ts
└─ config/env.ts                        (扩展：MySQL / 密钥 / 内部地址 / 路径)

pi-harness/tests/
├─ helpers/phase1.ts                    (真实 MySQL + 真实 Python 子进程)
├─ phase1-receipts.test.ts              (7)
├─ phase1-registry.test.ts              (9)
├─ phase1-e2e.test.ts                   (11)  ← §9 主表
├─ phase1-restart.test.ts               (2)   ← 真实进程 kill -9
├─ jwt-interop.test.ts                  (11)
└─ python/{test_internal_api_auth,test_history_dispatch}.py  (11 + 4)
```

---

## 2. python-impl 改动范围（白名单合规自查）

`git status --porcelain` 与 `git diff --stat` 实测：

```
 M .env.example          | +20  (新增变量注释)
 M api/main.py           | +41 −2
?? internal_api/         (新增)
?? migrations/           (新增)
```

| 白名单项 | 状态 |
|---|---|
| `internal_api/`（新增） | ✅ 4 个文件 |
| `migrations/`（新增） | ✅ 1 个文件 |
| `api/main.py` 子路由挂载（最小 diff） | ✅ +41/−2；含挂载 3 行与 §7 要求的 history 分发（分发本身在交接 §1.7 明确列入本次范围） |
| 根 `AGENTS.md` | ✅ `../AGENTS.md`（仓库根之外，git 不跟踪） |
| `.env.example`（新增变量注释） | ✅ |

**其他现有文件一律只读** ✅ —— 未修改 `auth/`、`platform_db/`、`checkpoint/`、`tests/`、`docker-compose.yml` 等任何文件。HEAD 仍为 `abf71d2`，未 commit / 未 push。

---

## 3. 设计 §9 验收用例结果表

| # | 场景 (F) | 结果 | 用例与关键输出 |
|---|---|---|---|
| 1 | **进程重启** (F12) | ✅ **PASS（真实 OS 进程）** | `tests/phase1-restart.test.ts`：spawn `src/server/main.ts` → 发一条消息 → `taskkill /f`（无优雅关闭、无 flush 机会）→ 重启新进程 → 续聊。断言：崩溃后 entry 数不变（已 durable）；重启后同一文件继续增长；`buildSessionContext()` 同时含「重启前的第一条消息」与「重启后的第二条消息」；`getSessionId()` 不变 |
| 2 | **容器重启** (F12) | ⚠️ **等价覆盖，非字面执行** | 见 §6-D3：`docker-compose.yml` 不在写白名单，无法登记 pi-harness 服务与持久卷，因此无法做 compose 级重启。已用 #1「真实进程 + 同一持久 session 目录」覆盖同一条恢复路径（durability 在 Pi append，恢复靠 reopen）。**不声称已做容器验证** |
| 3 | **重复 request_id 同 hash** (F7) | ✅ PASS | `tests/phase1-e2e.test.ts`：第二次响应 `replayed: true`、内容与首次一致；**Pi entry 数不变**（无新 turn）；`memory_source_event` 计数仍为 1（provenance 未重复）。复跑前提：faux 队列已清空，若真跑 prompt 会直接报错 |
| 4 | **重复 request_id 异 hash** | ✅ PASS | 同文件：同 id 换 payload → **409** |
| 5 | **同会话双请求并发** (F8) | ✅ PASS | 同文件：两个并发 POST 均 200，答复分别为 A/B 无交叉；entry 数恰好 **+4**（两个 turn，各 user+assistant，无丢失无重复）；`registry.stats().queued === 0`。另有 `tests/phase1-registry.test.ts` 断言 `maxConcurrent === 1` 与严格 FIFO 顺序 |
| 6 | **receipt 孤儿改判** | ✅ PASS | 同文件：把回执强置为 `processing`（模拟崩溃残留）→ 重发 → `replayed: false`、内容为「重跑后的答复。」、回执终态 `completed`。另有 `tests/phase1-receipts.test.ts` 覆盖 `failed_recoverable` 改判路径 |
| 7 | **idle 回收后续聊** | ✅ PASS | 同文件：`registry.disposeNow()` 后 `get()` 为 `undefined` → 续聊成功 → entry 数从原文件继续增长（历史完整）。另有 registry 用例断言 evict 时 `dispose()` 被调用且**不保存** |
| 8 | **权限** | ✅ PASS | 同文件：跨账户 session → 403/404；`harness_version=legacy` → **409**；错误签名 token → **401**（边缘拦截，无下游调用）。`tests/python/test_internal_api_auth.py` 另覆盖 disabled 账户 403、service token 缺失/错密钥/错 audience 401、TTL 超限 401、body 夹带身份 422 |

**8/8 通过**，无失败项。

---

## 4. 测试运行命令与最终结果

### 4.1 命令

```bash
# ---- Harness（TS）----
cd D:/Workspace_for_Codex/project005_SmartCS/pi-harness
npm ci                                   # 严格按 lockfile 安装
npx tsc --noEmit                         # 类型检查
npx vitest run                           # 全量：Phase 0 + Phase 1
npm run dev                              # 启动 HTTP 边缘（PORT 默认 8971）

# 单个 Phase 1 套件
npx vitest run tests/phase1-receipts.test.ts
npx vitest run tests/phase1-registry.test.ts
npx vitest run tests/jwt-interop.test.ts
npx vitest run tests/phase1-e2e.test.ts
npx vitest run tests/phase1-restart.test.ts

# ---- Python 侧验收 ----
cd python-impl
python -m pytest ../pi-harness/tests/python/ -q

# ---- 基线复核 ----
python -m pytest -q
```

**前置条件**：compose MySQL `:3307` 与 Redis 在运行；测试库 `smartcs_phase1_test` 存在
（创建一次：`CREATE DATABASE smartcs_phase1_test` + 授权 `smartcs` 用户）。
每个测试套件自行 `DROP/CREATE` 该库中的表并**执行真实迁移脚本**，不污染 `smartcs_checkpoint`。

### 4.2 最终结果

```
$ npx tsc --noEmit
(无输出 = 干净)

$ npx vitest run
 Test Files  17 passed (17)
      Tests  86 passed (86)
   Duration  62.83s

$ python -m pytest ../pi-harness/tests/python/ -q
15 passed in 45.99s

$ python -m pytest -q            # 基线，改动后
512 passed, 37 skipped, 1 warning in 111.81s

$ python -m pytest -q            # 基线，改动前（同一 session 早期运行）
512 passed, 37 skipped, 1 warning in 117.86s
```

新增测试分布：

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `tests/phase1-receipts.test.ts` | 7 | §3 状态机全分支（真实 MySQL） |
| `tests/phase1-registry.test.ts` | 9 | §4 串行/队列/超时/evict/先查后建 |
| `tests/jwt-interop.test.ts` | 11 | HS256 严格性 + 与 PyJWT 双向互通 |
| `tests/phase1-e2e.test.ts` | 11 | §9 主表 + SSE + history/DELETE |
| `tests/phase1-restart.test.ts` | 2 | 真实进程 kill -9 + 重启续聊 |
| `tests/python/test_internal_api_auth.py` | 11 | §6 契约全错误码 |
| `tests/python/test_history_dispatch.py` | 4 | §7 分发决策 |

### 4.3 迁移实测（真实 MySQL）

连跑 3 次（`run 1/2/3: OK`），最终结构：

```
conversation_session      + harness_version enum('legacy','pi') NOT NULL DEFAULT 'legacy'  (after title)
agent_run_receipt         id, session_id, client_request_id, request_hash, status,
                          response, open_write_operations, pi_revision_note, created_at, updated_at
                          indexes: PRIMARY(id), uq_request(session_id,client_request_id), idx_status(status,updated_at)
memory_source_event       event_id, session_id, business_user_id, client_request_id, content,
                          created_at, cleared_at
                          indexes: PRIMARY(event_id), uq_source_event_request(session_id,client_request_id),
                                   idx_session(session_id,created_at), idx_cleared(cleared_at)
```

### 4.4 端到端链路（非 mock）

`tests/phase1-e2e.test.ts` / `phase1-restart.test.ts` 的每一跳都是生产代码路径：

```
HTTP POST /api/chat
  → TS 本地验签 (jwt-hs256)
  → 子进程真 Python FastAPI (internal_api.auth)  ← 真实 MySQL
  → SessionRegistry (真互斥)
  → MySQL agent_run_receipt / memory_source_event
  → 真 Pi SessionManager (文件 transcript)
  → Faux provider            ← 唯一替身：模型本身
```

唯一替身是模型（Faux），这是离线可重复测试的必要条件，且是 Phase 0 已确立的做法。

---

## 5. 关键实现说明

- **为什么 HS256 是手写的**：算法固定为 HS256（无 `alg` 协商 → 无算法混淆类漏洞）、校验项可枚举、全量约 80 行可通读。可辩护性由 `tests/jwt-interop.test.ts` 支撑：11 个用例覆盖错密钥/篡改 payload/`alg:none`/`RS256` 替换/过期/超长 TTL/未来 iat/错 iss/意外 aud/缺 claim/畸形分段，并与**真实 PyJWT** 双向互通（Python 签 → TS 验；TS 签 → Python `decode_service_token` 验；错密钥 Python 拒收）。
- **reclaim 单写者依赖**：孤儿改判不做时间阈值，其安全性**完全来自 SessionRegistry 的 per-session 互斥**（Phase 1 无 WRITE，`processing` 必为崩溃残留）。该不变量写在 `src/db/receipts.ts` 头注释，并有边界用例固定：绕过互斥并发调用时，`complete()` 的条件 UPDATE 保证**至多一个调用方能提交响应**，管线把落败方映射为 409。
- **先查后建**：`SessionManager.findById` 命中后**再次校验** `existsSync` 与 `getSessionId()` 一致才 `open`，直接针对 Phase 0 A7 的「open 对缺失路径静默新建空会话」风险。
- **投影红线**：`projectHistory()` 用 `buildSessionProjection()` 应用 context edit，但**只遍历 active branch 的原始 entry**，compaction 摘要不进 UI 历史。

---

## 6. 偏差节（与计划/设计不一致之处，均未静默改文档）

**D1 — pytest 基线数字过期（需验收方知悉）**
交接指令与设计写的 367/18，实测为 **512/37**。已按实测值复核（改动前后一致）。建议更新文档中的基线数字。

**D2 — DDL 列类型按现有 schema 调整（3 处）**

| 设计草案 | 实际采用 | 原因 |
|---|---|---|
| `session_id CHAR(36)` | `VARCHAR(128) COLLATE utf8mb4_bin` | `conversation_session.session_id` 实为 `VARCHAR(128)`；保持可 join / 可 FK |
| `user_id BIGINT -- business_user_id` | `business_user_id VARCHAR(128) COLLATE utf8mb4_bin` | `platform_user.business_user_id` 实为 `VARCHAR(128)` 字符串；按 BIGINT 建会立刻类型不符 |
| `client_request_id VARCHAR(64)` | `VARCHAR(128) COLLATE utf8mb4_bin` | 与 `conversation_session.client_request_id` 及 `_identity()` 的 128 上限一致 |

**D3 — compose 增量与「容器重启」用例未做（受写白名单限制）**
设计 §8 把 compose 增量列为交付物（「本阶段可先本地开发态，compose 改动随 Phase 1 报告评估」），但交接指令 §2 的写白名单**不含 `docker-compose.yml`**，本次未修改。其后果是设计 §9 的「容器重启」无法字面执行。已用等价路径覆盖（真实进程 kill -9 + 同一持久 session 目录），但**不声称已完成容器级验证**。请验收方裁决：或在后续 phase 放开 compose 写权限，或明确接受以进程级重启作为该行的验收。

**D4 — `memory_source_event` 增加唯一键（超出设计草案）**
设计 §2 草案只有 PRIMARY + idx_session + idx_cleared。实际增加 `UNIQUE KEY uq_source_event_request (session_id, client_request_id)`，使 provenance 写入在重试下幂等（配合 `ON DUPLICATE KEY UPDATE`）。否则同一 request 被重跑会留下重复 provenance 行。

**D5 — Service token 的 `business_user_id` 改为可选（与计划 §7 字面不同）**
计划 §7 把 `business_user_id` 列为 Internal Service JWT 的必备 claim，但 `/internal/auth/verify` 正是**解析**该值的那次调用，存在先后矛盾。处理：该 claim 在 verify 调用中**可省略**；一旦出现则与数据库交叉校验（不匹配 → 401）。account_id / session_id / client_request_id 仍为必备。自 Phase 2（真实工具调用）起恢复为必备。Python 与 TS 两侧同步实现，并有互通测试。

**D6 — 新增额外的 token-session 绑定（设计未要求）**
`/internal/auth/verify` 额外强制 `service_token.session_id == body.session_id` 且 `service_token.account_id == user_jwt.account_id`，否则 401。属防御性增强（服务凭证只能为其自身声明的会话背书），有测试固定。

**D7 — 新增环境变量 `PI_HARNESS_BASE_URL`**
设计 §7 要求 Python 转发 history 到 TS，但设计 §8 的 env 清单未列出 Python 侧需要的 harness 地址。新增 `PI_HARNESS_BASE_URL`（已写入 `.env.example`）。

**D8 — Python 侧测试放在 `pi-harness/tests/python/` 而非 `python-impl/tests/`**
交接指令 §2 的写白名单不含 `python-impl/tests/`，故 15 个 Python 验收用例放在 `pi-harness/tests/python/`，用显式路径运行（`pytest.ini` 的 `testpaths = tests` 保证它们**不会**进入 `python -m pytest -q` 基线）。代价：它们不在标准套件内。**建议**后续 phase 放开 `python-impl/tests/` 写权限后迁回。

**D9 — Receipt 层自身不保证并发安全（依赖组合约定）**
设计 §3 的「无条件改判孤儿」+ §6.2 的互斥是**组合**约定：脱离 SessionRegistry 直接并发调用 `beginRequest` 时，两个调用方**都可能被指派重跑**（`tests/phase1-receipts.test.ts` 的 BOUNDARY 用例固化了这一点）。真正的保护是 `complete()` 的条件 UPDATE（至多一方提交）。已写入代码头注释与 §5，避免后续有人绕开 registry 直接复用该仓储。

**D10 — `close()` 会强制断开在途 SSE 连接**
Shutdown 顺序为 `registry.shutdown()` → `closeIdleConnections()` → `close()` → `closeAllConnections()`。Phase 1 无优雅排空要求，若后续需要，应在 Phase 6（可观测）或灰度阶段补 graceful drain。

**D11 — 服务凭证来源**
TS 侧配置读取顺序为 `process.env` → `python-impl/.env`，即两侧共享同一份密钥文件（便于轮换一次生效）。`.env.example` 已加说明；真实 `.env` **未修改**（不在白名单）。

**D12 — 修正 AGENTS.md 一处既有错误**
原文写 `agents/` 是「LangGraph agents」，但计划 §1 明确指出代码库**没有任何 LangGraph**（`scripts/check_repository_readiness.py:30` 明令禁止）。本次更新 AGENTS.md 时一并更正为「显式 async ChatOrchestrator 及其 handlers」。

**D13 — `SessionRegistry.acquire` 之外的接口扩展**
设计 §4 定义了 `acquire/get/scheduleIdleEviction/stats`，均按要求实现；另加 `tryAcquire`（DELETE 需「活跃即 409」而非排队）、`withExclusive`、`disposeNow`、`ensureSession`、`originOf`、`shutdown`。均为超集，未改变设计语义。

---

## 7. 遗留与移交

1. **裁决 D3**：compose 增量与「容器重启」验收方式（放开写权限 / 接受等价覆盖）。
2. **裁决 D8**：是否将 Python 验收用例迁入 `python-impl/tests/`。
3. **文档数字**：`HANDOFF-phase1.md` / `phase1-design.md` / 计划中「367 passed / 18 skipped」应更新为 512/37。
4. **Phase 2 前置**：接入只读业务工具时，Internal Service JWT 的 `business_user_id` 恢复必备（D5），并需为 `/internal/tools/execute` 定义契约。

---

## 8. 硬约束合规自查

| 约束 | 状态 |
|---|---|
| 不接真实 WRITE / 真实业务工具 | ✅ 工具集维持 Phase 0 的 2 个 fake 只读工具 |
| 不建 pending_action / 不拆 RAG / 不动公网 auth 语义 | ✅ 未触碰 |
| python-impl 改动限白名单 | ✅ 见 §2（实测 git 输出） |
| pytest 基线不破坏 | ✅ 512/37 改动前后一致 |
| 不 commit / 不 push | ✅ HEAD 仍 `abf71d2` |
| MySQL 真实验证，不伪造 | ✅ 真实 :3307；未使用 SQLite 替代 |
| 测试可重复、命令入报告 | ✅ §4.1 |
| 全绿或如实列失败 | ✅ 无失败项；D3 的未覆盖项已如实标注 |
| 与设计冲突记入偏差、不静默改文档 | ✅ §6，13 项；计划/设计文档零修改 |

---

**STATUS: completed**

- 设计 §9 验收：**8/8 通过**（「容器重启」以等价路径覆盖并已标注，见 D3）
- TS：**86 passed / 0 failed**（17 文件），typecheck 干净
- Python Phase 1：**15 passed**
- pytest 基线：**512 passed / 37 skipped**（改动前后一致）
- 迁移：真实 MySQL 上连跑 3 次幂等
- 未 commit / 未 push / 未越白名单
