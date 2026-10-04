# Phase 7 报告：Cohort 灰度（分桶 + 统一入口 chat 分发）

> **执行方**：Claude Code（本终端，Phase 7 执行轮） ｜ **日期**：2026-10-04
> **依据**：`../python-impl/docs/HANDOFF-phase7.md` + `../python-impl/docs/phase7-design.md` + `pi-replatform-plan-v2.md` §10 Phase 7 / §13
> **前置**：Phase 0–6 与 6b 全部验收通过（TS 138/27 files、pytest 585/37、`tsc` 干净）
> **STATUS: completed**

---

## 0. 摘要

Phase 7 的三件事全部落地，并且每一件都有**实测证据**而非推断：

| 目标 | 落地 | 证据 |
|---|---|---|
| 灰度开关 + 确定性分桶 | `SMARTCS_PI_ROLLOUT_PERCENT`（0–100，默认 0，非法值启动即失败）；`harness_version_for_account()` = SHA-256(版本化盐 + account_id) 取模，**不用 Python `hash()`** | §3：三个不同 `PYTHONHASHSEED` 的独立进程给出逐个账户相同的判定；0/10/25/50/75/100% 的实测占比 0/9.4/24.7/50.0/74.5/100% |
| 创建即固定、终身不变 | `platform_db/sessions.py` 的 create 写入 `harness_version`（最小 diff）；`ON DUPLICATE KEY` 重试命中旧行**不改写**；`touch()` 不涉及该列 | `test_pi_session_pinning.py`：同 `client_request_id` 二次创建（故意换版本）返回同一 session 且版本不变；P7-4：已存在 legacy 会话在 PERCENT=100 下仍走 legacy |
| 统一入口 chat 分发 | `api/main.py::chat` 按 session 的 `harness_version` 分流：legacy → 原 orchestrator（一行未改），pi → `forward_chat` 转发 TS；**转发失败 503，绝不降级** | §4：真实 `main.chat` → 真实 HTTP → 真实 harness 进程 → 真实 Python internal 路由 → Faux 模型 → MySQL 回执，端到端跑通；harness 被杀后同一入口 503 且 legacy orchestrator 的 tripwire 计数为 0 |

**红线守住了两条**：① 分桶只发生在**会话创建**这一处，任何读路径/后续请求都不会重算或改写既有会话的 harness（P7-4 用「PERCENT=100 下的老 legacy 会话」正面验证）；② pi 会话永不落 legacy orchestrator——不可达时 503，宁可失败也不产生双 transcript 体系（P7-6 的 tripwire 是硬门禁）。

---

## 1. 交付物对照表（设计 §6）

| 设计 §6 要求 | 落点 | 状态 |
|---|---|---|
| 灰度开关 `SMARTCS_PI_ROLLOUT_PERCENT` | `internal_api/harness_client.py::rollout_percent()` + `.env.example` 明文说明 | ✅ 启动时校验（`api/main.py` lifespan 记录并 fail-fast 非法值） |
| 确定性分桶（禁 `hash()`） | `internal_api/harness_client.py::harness_version_for_account()`（SHA-256 + 版本化盐 `smartcs-pi-rollout:v1`） | ✅ 跨进程/跨 hash seed 恒定（§3） |
| 会话创建写死 harness_version | `platform_db/sessions.py::create(..., harness_version="legacy")`（仅 create 路径） | ✅ 两个创建入口（`POST /api/sessions`、chat 隐式创建）共用 `api/main.py::_create_session` |
| 统一入口 chat 分发 | `api/main.py::chat` + `_pi_chat` + `_pi_chat_response` | ✅ pi → 转发；legacy → 原链路；503 不降级 |
| chat 转发客户端 | `internal_api/harness_client.py::forward_chat()`（用户 JWT 原文 + `X-SmartCS-Service-Token` 服务签名头） | ✅ 签名头由 TS 侧校验（存在即必须有效，`app.ts::verifyForwardedServiceToken`） |
| 响应适配 | TS `intent_label` → Python `ChatResponse.intent`；`compliance_passed` 语义见 §6 偏差 3 | ✅ `harness_version` 随响应返回（工作台可见） |
| 用例 P7-1～P7-7 | `python-impl/tests/test_pi_rollout_bucketing.py`、`test_pi_session_pinning.py`、`test_pi_chat_dispatch.py`、`test_pi_unified_entry_e2e.py`、`pi-harness/tests/phase7-unified-entry.test.ts` | ✅ 新增 51 项 Python + 4 项 TS（585→636） |
| `pi-harness/PHASE7_REPORT.md` | 本文件 | ✅ |

---

## 2. P7-1 ～ P7-7 逐项结果

| # | 场景 | 判定方式 | 结果 |
|---|---|---|---|
| **P7-1** | PERCENT=0：新会话全部 legacy，legacy 链路端到端正常 | 分桶单测（200 个账户全 legacy）+ 真实 handler：PERCENT=0 下隐式创建 → DB 行 `legacy`、orchestrator 被调用、`forward_chat` 零调用 | ✅ |
| **P7-2** | PERCENT=100：新会话全部 pi，经统一入口转发 TS 全链路通 | 真实 `main.chat` → 真实 `forward_chat` HTTP → 真实 harness 进程 → 真实 Python internal 路由 → Faux 模型：回答、`intent_label`、`harness_version="pi"`、回执 `completed`、provenance 一行；同 `client_request_id` 重发**不新增**任何行 | ✅ |
| **P7-3** | 分桶确定性（同账号反复建会话 + 跨进程重启恒定） | 同账号 5 次真实创建同版本；4 个独立进程 × 3 种 `PYTHONHASHSEED` 输出逐账户一致（§3） | ✅ |
| **P7-4** | 旧会话不变：PERCENT=100 下已存在 legacy 会话仍走 legacy，版本不被改写 | 真实 handler + 真实 DB：legacy 会话在 PERCENT=100 下仍进 orchestrator、`forward_chat` 零调用；`touch()` 后版本列不变；重试命中旧行时（故意传 `pi`）仍返回 `legacy` | ✅ |
| **P7-5** | 混合灰度：PERCENT=50 两类账号并存、互不串扰 | 真实 DB 播种到同时存在两类账户 → 两条会话各自路由：pi 会话命中 forward（1 次）、legacy 会话命中 orchestrator（1 次），互不交叉 | ✅ |
| **P7-6** | pi 不可达：转发失败 → 503，**不降级**；恢复后同请求可重试 | 单测：`HarnessUnavailable` → 503 且 tripwire 计数 0；E2E：真的杀掉 harness 进程 → 同入口 503、回执表不增长、tripwire 计数 0；重启 harness 后**同一请求**重试成功 | ✅ |
| **P7-7** | 基线 | `python -m pytest -q`、`npx vitest run`、`npx tsc --noEmit`；`agents/` 等业务目录零改动 | ✅ pytest **636/37**、vitest **142/28 files**、`tsc` 干净（§7） |

---

## 3. 分桶证据（P7-3：跨进程重启恒定）

三个独立进程、不同 `PYTHONHASHSEED`（若实现里用了 Python `hash()`，这里必然发散）：

```text
$ SMARTCS_PI_ROLLOUT_PERCENT=50 PYTHONHASHSEED=0     python -c "...v(a) for a in (1,7,42,100,1234,99991)"
PYTHONHASHSEED=0   PERCENT=50 -> 1:legacy 7:pi 42:pi 100:pi 1234:pi 99991:pi
PYTHONHASHSEED=1   PERCENT=50 -> 1:legacy 7:pi 42:pi 100:pi 1234:pi 99991:pi
PYTHONHASHSEED=999 PERCENT=50 -> 1:legacy 7:pi 42:pi 100:pi 1234:pi 99991:pi
```

占比随开关单调扩张（同一账户一旦入组，扩大灰度只会继续留在组内）：

```text
PERCENT=0   -> pi=0.0%    (first pi account id: None)
PERCENT=10  -> pi=9.4%    (first pi account id: 17)
PERCENT=25  -> pi=24.7%   (first pi account id: 15)
PERCENT=50  -> pi=50.0%   (first pi account id: 7)
PERCENT=75  -> pi=74.5%   (first pi account id: 2)
PERCENT=100 -> pi=100.0%  (first pi account id: 1)
```

回归钉（`test_buckets_are_pinned`）：盐或取模一旦改动，这组账户的归属会变，测试会当场失败——灰度重排必须是显式决定，不能悄悄发生。

---

## 4. 统一入口转发实测（P7-2 / P7-6 证据）

`tests/test_pi_unified_entry_e2e.py`（**唯一被替换的是模型本身**，Faux，离线）：

```text
api.main.chat（真实 handler）
  → internal_api.harness_client.forward_chat（真实 HTTP，带用户 JWT 原文 + 服务签名头）
    → pi-harness /api/chat（真实进程：matrix-harness 生产装配 + Faux 模型 + 真实回执表）
      → POST /internal/auth/verify（真实 uvicorn 上的真实 internal 路由，真实 MySQL）
```

断言全部读**权威**而不是读 harness 的自述：

| 断言 | 观测点 |
|---|---|
| 回答与 intent 来自 harness | `ChatResponse.response == "E2E 回答：订单已发货。"`、`intent == "order"`（TS `classifyIntent`）、`harness_version == "pi"` |
| 会话创建即固定 | `conversation_session.harness_version == 'pi'`（PERCENT=100 分桶命中） |
| 真跑了一轮 | `agent_run_receipt` 一行、`status='completed'`；`memory_source_event` 一行 |
| 回执幂等 | 同 `client_request_id` 重发 → 回答相同，`memory_source_event` 仍**一行**（回执命中发生在 provenance 写入之前） |
| 不降级 | harness 进程被杀后仍 503；legacy orchestrator tripwire 计数 **0** |
| 可重试 | 重启 harness 后**同一请求**重试 → 200 且回执表出现该 request id |

TS 侧 `tests/phase7-unified-entry.test.ts` 补齐转发形态的两面：带合法服务签名头的转发被接受并返回 `intent_label`；**被篡改**或格式错误的签名头 401 且不产生 provenance 行；不带该头部的直连（Phase 1–6 契约）行为不变。

---

## 5. 本阶段改动文件

| 文件 | 性质 |
|---|---|
| `python-impl/platform_db/sessions.py` | **授权内最小 diff**：`create()` 新增 `harness_version` 形参（默认 `legacy`）+ 取值校验 + INSERT 列；重试命中旧行不改写 |
| `python-impl/internal_api/harness_client.py` | 新增 `rollout_percent()` / `harness_version_for_account()` / `forward_chat()`；文件头补 Phase 7 说明 |
| `python-impl/api/main.py` | `_create_session()` 统一创建入口（分桶 + 观测日志）；`chat` 分流（`_pi_chat` / `_pi_chat_response` / `_request_user_token` / `_harness_detail`）；`ChatResponse.harness_version`；lifespan 校验并记录灰度配置 |
| `python-impl/.env.example` | `SMARTCS_PI_ROLLOUT_PERCENT=0` + 语义说明 |
| `pi-harness/src/server/app.ts` | `/api/chat` JSON 增加 `intent_label`；`X-SmartCS-Service-Token` 存在即校验（`verifyForwardedServiceToken`） |
| `python-impl/tests/test_pi_rollout_bucketing.py` | **新增**（25 项） |
| `python-impl/tests/test_pi_session_pinning.py` | **新增**（6 项） |
| `python-impl/tests/test_pi_chat_dispatch.py` | **新增**（19 项） |
| `python-impl/tests/test_pi_unified_entry_e2e.py` | **新增**（1 项跨服务 E2E，node 缺失时 skip） |
| `python-impl/tests/test_api_rag_smoke.py` | **返修：测试替身接口对齐 2 行**（`_SmokeSessions.create` 增加 `harness_version` 形参 + 行字段；断言零改动；经验收侧批准，见 §6 偏差 9） |
| `pi-harness/tests/phase7-unified-entry.test.ts` | **新增**（4 项） |

`agents/`、`memory/`、`mcp/`、`web/`、`auth/`、`context/`、`checkpoint/` 等业务目录**零改动**；legacy orchestrator 一行未动。

---

## 6. 偏差与限制

1. **`migrations/001` 是部署前置条件（重要）**：chat 隐式创建现在会写 `harness_version` 列，未执行 Phase 1 DDL 的库会在建会话时报错（被包装为 503 platform database unavailable）。实测本机只有 `smartcs_phase1_test` 有该列，`smartcs_checkpoint`（开发库）**没有**——所以要跑真实服务前必须先执行 `migrations/001_phase1_session_foundation.sql`（脚本幂等，可重跑）。这与「本阶段不包含 compose 容器化、迁移留给终局交付轮」一致，但必须在交付轮显式排入。
2. **服务签名头是「存在即校验」而非「必须存在」**：Phase 1–6 的直连 edge 契约（仅用户 JWT）保持不变，因此没有把 `X-SmartCS-Service-Token` 变成硬门槛。要改成强制（即关闭浏览器直连 TS 的路径）属于 edge 契约变更，应单独排期；本阶段先把校验通路与测试钉死（篡改即 401）。
3. **pi 会话的 `compliance_passed` 恒为 `true`**：合规闸门在 harness 的 `message_end` 扩展内（Phase 3），能到达 edge 的最终答复要么通过、要么已被安全兜底替换；harness 未在上行响应里回报「本条被替换过」。若工作台需要区分「已拦截」，需要 TS 侧新增字段——属后续变更。
4. **pi 会话的 `intent` 用的是 harness 的观测标签**（`refund/ticket/order/risk/knowledge/general`），与 legacy 的中文业务 intent 词表不同；工作台 `formatIntent` 对未知值原样展示，不报错。灰度期同一账户可能看到两种标签风格——这是两条链路观测口径的既有差异，不是本阶段引入的。
5. **`/api/checkpoints/{id}/resume` 对 pi 会话无意义**：pi 会话没有 legacy checkpoint，该端点返回 404，**不写入任何 legacy 状态**（不会造成双体系）。本阶段未改动它。
6. **`forward_chat` 超时 120s**（内部调用默认 10s 不适用于含模型时延的整轮）；超时按不可达处理 → 503。
7. **E2E 用例在缺少 node/tsx 时 skip**：跨服务用例需要 `node` 与 `pi-harness/node_modules/tsx`，缺失时明确 skip 而不是让默认套件变红；本机实测**真实执行**（非 skip）。
8. **新增用例使套件变慢**：4 个新模块每次用 `apply_migration()` 重置测试库（沿用既有 internal_api 用例的隔离方式），合计约 +130s。这是隔离换来的成本，未做模块级共享。
9. **返修：/api/chat 重构打破两条既有测试的接缝（真实回归，已修）**：验收侧在独占窗口的全量 pytest 中报出 3 项失败，全部落在本轮改动的 chat 接缝上，逐条定位如下（实测 traceback，非推断）：

   | 失败 | 真实机制 | 处置 |
   |---|---|---|
   | `test_context_integration.py::test_chat_routes_preserve_context_errors_for_safe_global_handler[chat]`、`[legacy-chat]` | 该用例**位置调用** `api.chat(ChatRequest(...), user)`；我把签名从 `(body, user)` 改成 `(body, request, user)` 后，第二个位置参数被绑到 `request`，`user` 落到 `Depends` 默认值 → `AttributeError` | **代码侧修复**：改为 `chat(body, user=Depends(...), user_token=Depends(_forward_user_token))`——转发所需的原始 token 做成 FastAPI 注入依赖，处理器不再持有 `Request`，`(body, user)` 位置调用语义恢复；legacy 路径零影响 |
   | `test_api_rag_smoke.py::test_api_chat_rag_and_capability_smoke` | 测试替身 `_SmokeSessions.create()` 是旧接口，新增的 `harness_version=` 实参触发 `TypeError` | **改测试替身 1 行**（签名 + 行字段），不动任何断言；属接口变更后的替身对齐，不削弱用例 |

   说明：验收侧初判的根因（`_owned_session` 桩失效）与实测不符——legacy 路径 `_owned_session` 仍是接缝且仍被调用。另注：位置调用这一条本可在**模块级**运行中暴露，我此前只跑了自研新模块，未跑既有 chat 相关模块，是自查覆盖的缺口，已由本轮返修补上（修后 `test_pi_chat_dispatch.py` 19 项 + 该 3 个参数化用例全绿）。

   **替身修改的裁决记录（验收侧 python-impl-79 于本轮批准，原文要点）**：① `Sessions.create` 是生产接口，Phase 7 设计 §4 已授权其新增 `harness_version` 参数，接口演进后测试替身跟随接口属常规维护，不构成「削弱测试」（该用例测 RAG 冒烟，与灰度无关）；② 否决「把分桶下沉到存储层」方案（违反设计分层：分桶归 api/main.py，存储层不得反向依赖灰度策略）；③ 否决「加 RolloutSessions 生产包装器」方案（为迁就替身引入生产间接层）。**条件与授权范围**：仅此两行（签名 + 行字段）、断言零改动；作为对既有 legacy 测试文件改动的先例，授权范围严格限于「接口演进引起的替身适配」。
10. **共享测试库窗口发生互踩（过程事实，非代码问题）**：本机同一时段存在另一个 runner（`npx vitest run` 15:12 起、`python -m pytest -q` 15:21 起），与本轮的全量 pytest 在 `smartcs_phase1_test` 上重叠，双方都出现与代码无关的失败/挂起（`tests/README.md` 描述的场景，与 Phase 6 §偏差 9 同源）。**处置**：本报告的所有 ✅ 证据来自各**模块级**运行（互踩前/互踩外，均已单独复跑通过）与**协调后的独占窗口全量重跑**；被污染的那两次全量运行不作为结论依据。各 runner 的最终基线应由各自独占窗口内的运行给出。

---

## 7. 基线（独占窗口实测）

```text
$ python -m pytest -q
636 passed, 37 skipped, 3 warnings in 428.82s (0:07:08)

$ npx vitest run
 Test Files  28 passed (28)
      Tests  142 passed (142)
   Duration  490.86s

$ npx tsc --noEmit
（无输出，干净 — 本轮已在本机复跑两次）
```

- pytest：585（6b 基线）→ **636 passed / 37 skipped**，+51 = 本阶段新增（分桶 25、会话固定 6、chat 分发 19、跨服务 E2E 1）；0 失败。
- vitest：138/27 files（6b 基线）→ **142 passed / 28 files**，+4 = 本阶段新增（`tests/phase7-unified-entry.test.ts`）；0 失败。
- 两次运行同在**一个独占窗口**内完成（15:40:34 → 15:56:09），期间无第二个 runner 触碰 `smartcs_phase1_test`。
- 上述 3 条 warning 为既有噪音（OTLP 端点未起时的导出提示 + `websockets` 弃用告警），非本阶段引入。

| 项 | 状态 |
|---|---|
| 白名单 | ✅ 业务代码仅 `platform_db/sessions.py`（create 路径）、`internal_api/harness_client.py`、`api/main.py`、`.env.example`；测试侧新增 4 个文件 + `test_api_rag_smoke.py` 的 2 行替身对齐（经批准，§6 偏差 9）；`pi-harness` 侧 `src/server/app.ts` + 新测试 |
| 业务目录零改动 | ✅ `agents/` 等未触碰（`git status --porcelain` 中修改项只有上述文件） |
| 不 commit / 不 push | ✅ HEAD 仍 `abf71d2` |
| 机器纪律 | ✅ 无残留 node/python 子进程；临时目录由 pytest `tmp_path` 管理；占用测试库窗口期间未并发跑第二套 |
| 未降级表述 | ✅ 所有 ✅ 均附实测输出或权威读数（DB 行 / 进程行为），无「应该」「推断」 |

---

**STATUS: completed**

- **分桶**：SHA-256 确定性，跨进程/跨 hash seed 恒定，回归钉防重排；PERCENT 0/10/25/50/75/100 实测占比 0/9.4/24.7/50.0/74.5/100%
- **固定**：harness_version 仅在建行时写入，重试/touch/灰度调整都不改写；PERCENT=100 下老 legacy 会话正面验证仍走 legacy
- **统一入口**：真实 `main.chat` → 真实 harness 进程全链路实测通过（回执幂等、intent 适配、harness_version 标记）；harness 不可达 503 且 tripwire 证明**未降级**；重启后同请求可重试
- **基线（独占窗口 15:40:34–15:56:09）**：pytest **636 passed / 37 skipped**（+51）、vitest **142 passed / 28 files**（+4）、`tsc` 干净；白名单与业务目录约束均满足；未 commit / 未 push
- **返修**：验收侧报出的 3 项真实回归已全部修复（2 项代码侧 + 1 项经批准的测试替身对齐），修后全量双绿；裁决理由与授权范围见 §6 偏差 9

PHASE7_DONE completed
