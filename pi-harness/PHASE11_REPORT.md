# Phase 11 报告：最终收口（仓库形态 + 运维就绪）

> **日期**：2026-10-05 ｜ **依据**：`docs/HANDOFF-phase11.md`（含 6 条裁决）+
> `docs/SmartCS_Final_Closeout_Plan_Repository_Operational_Readiness.md`
> **前置**：Phase 10 验收通过；`pi-harness` 已由验收方以 subtree 并入仓库（commit `18fa087`）。
> **本轮未 commit / 未 push**（裁决 6）；工作树改动由验收方统一提交。

---

## 0. 终态一句话

```text
仓库   一个 Git root，两套运行时：python-impl/{pi-harness, Python Business Runtime}
运行   /health = 活着    /ready = 能服务（MySQL + Python /internal/ready [+ MCP Gateway]）
Outbox 可观察（stats + /internal/ops/memory-outbox + 进程计数）
       可恢复（CLI failed→pending，投递仍由 dispatcher 完成）
```

---

## 1. ① Monorepo 路径适配

Sibling 假设已清零。逐文件说明：

| 文件 | 改动 | 为什么 |
|---|---|---|
| `compose.yaml` | `context: ../pi-harness` → `./pi-harness`；env 挂载默认值 `../python-impl/.env` → `./.env`；注释同步 | 仓库根现在同时是 Python 根与 compose 上下文根 |
| `pi-harness/src/config/env.ts` | 删除 `WORKSPACE_ROOT`，新增 `PYTHON_IMPL_ROOT = resolve(PI_HARNESS_ROOT, "..")`；两处 `.env` 解析改为 `resolve(PYTHON_IMPL_ROOT, ".env")` | 共享 `.env` 从「上跳到 sibling」变成「上一级到仓库根」 |
| `pi-harness/tests/helpers/phase1.ts` | `PYTHON_IMPL = PYTHON_IMPL_ROOT`；`resolve` 导入移除；错误文案改「repository-root .env」 | 同一个根，四处派生路径（migrations / PYTHONPATH / cwd）随之正确 |
| `pi-harness/scripts/legacy_probe.py` | `parents[2] / "python-impl"` → `parents[2]`；usage 文案 | `parents[2]` 本身就是仓库根 |
| `pi-harness/scripts/phase4b-real-shadow.ts` | 注释：`python-impl/.env` → `repository-root .env` | 不改行为，只改描述 |
| `pi-harness/README.md` | 4 处路径：runtime-boundaries / `.env` 复制 / "Python-side acceptance 在 tests/python/" / architecture.md | 后两条同时是**事实错误**（见 §8-D1） |
| `pi-harness/tests/README.md` | `python-impl/.env` → 仓库根 `.env` | 同上 |
| 根 `README.md` | 首屏重写为双层；`../pi-harness/*` → `pi-harness/*`；项目结构树补 `pi-harness/`；compose 行描述 | Phase 3 要求 |
| `docs/architecture.md` | `../../pi-harness/*` → `../pi-harness/*` | docs/ 到仓库根是一级 |
| 根 `.dockerignore` | 新增 `pi-harness/` | Python 镜像构建上下文是仓库根，否则会把 Node 树（含 node_modules）打进镜像 |

**未改的文件**：`pi-harness/Dockerfile`（只 COPY 自身 `package.json/src/skills`，无 sibling 假设）、
根 `Dockerfile`（`COPY . .` + 上面的 `.dockerignore` 已覆盖）、`docs/runtime-boundaries.md`
与 `docs/recovery.md`（grep 无 sibling 路径）。运行中的服务**未受影响**（它们从旧 sibling 目录启动）。

### 1.1 过程中发现并修掉的真实缺陷（D-A）

第一版把 `PYTHON_IMPL_ROOT` 直接赋值成 `PI_HARNESS_ROOT`。那是 `pi-harness/` **自身**目录
（`src/config` 上跳两级），不是仓库根——于是 `.env` 解析指向 `pi-harness/.env`（不存在）。
`npm ci` 之后的 DB 测试立刻以 `Error: MYSQL_PASSWORD is required` 失败，暴露出这一点。
修正为 `resolve(PI_HARNESS_ROOT, "..")` 后全部通过。

> 记在这里是因为它正是「路径修复必须在仓库内跑真实测试才算数」的例证：单看 diff
> 完全合理，`tsc` 也干净，只有真实连接才把它打出来。

---

## 2. ② Harness `/ready`

新增 `pi-harness/src/server/readiness.ts` 与 `GET /ready`（`src/server/app.ts`）。

```text
GET /health   liveness  原样不动：{ ok: true, sessions }
GET /ready    readiness
  checks.mysql              SELECT 1（ReceiptStore.ping()），不跑 migration、不写
  checks.business_runtime   GET /internal/ready（新增，见 §2.1）
  checks.mcp                enabled = (transport === "mcp")；enabled 时才查 Gateway /health
```

- **503 只由依赖不可达触发**（MySQL / Business Runtime / mcp 模式下 Gateway）。
- **memory backlog 不拖死 readiness**：`pending > 0` → `ready:true, degraded:true,
  warnings:["memory_outbox_backlog"]`。
- 响应只有布尔与告警码；不返回 URL、密钥、JWT、SQL 或驱动文本（有测试断言）。
- 探针自身抛异常也答 503「not ready」，不答 500（编排系统会把 500 当成崩溃循环）。
- 总预算 3s，每个依赖各自有界——挂死的依赖不会挂死探针。
- compose 的 harness healthcheck 改打 `/ready`；`/health` 保留给人工与进程 liveness。

### 2.1 Python `GET /internal/ready`（新增 `internal_api/ready.py`）

```json
{ "ok": true, "platform_db": true, "tool_runtime": true, "memory_runtime": true }
```

- `platform_db`：新增 `PlatformDatabase.ping()`，一条连接一条 `SELECT 1`。
- `tool_runtime`：executor 已接线 + 新增 `OrderRepository.ping()`（`SELECT 1`，
  刻意不用 `count_orders()`——readiness 不该随业务数据增长而变慢），走 `asyncio.to_thread`。
- `memory_runtime`：记忆服务已初始化且其存储（同一个平台库）可达。
- **便宜是硬约束**：不调 LLM、不跑 RAG、不执行订单/退款/工单。有测试用「调用即抛异常的
  executor」钉住这一点。
- 认证沿用内部通道：但该端点**不属于任何一轮对话**，因此新增
  `decode_ops_service_token`（校验同一 secret/aud/iss/iat/exp/TTL，不要求
  `account_id`/`session_id`）。不为凑合既有解码器而伪造一轮身份。
- `ok` 取三项的**合取**；返回 200 / 503。

### 2.2 MCP Gateway `GET /health`

`internal_api/mcp_gateway.py` 新增 `HealthEndpoint`，**置于 `BearerTokenGuard` 之内**：

- 200 同时证明：ASGI 已启动 · guard 已武装 · token 正确 · `create_server` 已返回
  （MCP server 与 retriever 就在那里构建）。
- 不发任何检索；不做搜索就叫 readiness。
- 该端点要求 token：与 harness 的探针约定一致，「分不清健康与 token 错误」的健康检查毫无用处。
- 未动 compose 里 gateway 既有的 401-at-`/mcp` healthcheck（仍然成立，且改动无收益）。

---

## 3. ③ Outbox 观测 + 人工恢复

### 3.1 持久层（`ReceiptStore`）

- `ping()`：`SELECT 1`。
- `memoryOutboxStats()`：一条聚合查询，返回
  `pending` / `failed` / `oldestPendingAgeSeconds` / `maxPendingAttempts`。
  - `WHERE status='completed' AND memory_enqueue_status IN ('pending','failed')`，
    走既有索引 `idx_memory_outbox (memory_enqueue_status, updated_at)` 做**有界范围扫描**。
  - 年龄由 MySQL 用 `NOW(3)` 计算，不比较 Node 时间戳——两台主机之间没有时钟偏差。
  - **不数 `done`**：那一侧随收据总量无界增长，正是这条查询唯一不能碰的东西
    （方案 §6.1 把 `done_count` 列为可选项并明确警告不要扫大表）。
- `requeueMemory(receiptId)`：`failed → pending` 且 `attempts = 0`，**只此一个 effect**；
  条件 UPDATE，非 failed 行是 no-op。
- `listFailedMemory(limit)`：`--failed` 的选择集，最旧优先。

### 3.2 Dispatcher 进程指标（`OutboxProcessStats`）

`passes` / `delivered` / `deliveryFailures` / `parked` / `lastDispatchAt` /
`lastSuccessAt` / `lastErrorAt`。明确标注**非权威**（重启归零）；权威永远是收据行。
「连队列都读不到的那一趟」不计入 `passes`，但记 `lastErrorAt`。

### 3.3 内部接口

```text
GET /internal/ops/memory-outbox        （Service JWT 保护，只读）
  durable     { pending, failed, oldest_pending_age_seconds, max_attempts }
  dispatcher  { passes, delivered, delivery_failures, parked, last_dispatch_at,
                last_success_at, last_error_at } | null
```

只观察：不改收据、不 retry、不执行 LLM / Tool。无凭据 401（有测试）；MySQL 不可达 503。
线上字段为 snake_case（与 `durable` 块及方案示例一致），TS 类型仍用 camelCase。

### 3.4 CLI

```bash
npm run outbox:status
npm run outbox:retry -- --receipt-id 123
npm run outbox:retry -- --failed --limit 10
```

- 行为仅 `failed → pending, attempts = 0`，然后交回**正常 dispatcher**。
- CLI **只 import 数据库层**——不 import python-client / agent / tools / dispatcher，
  因此它在结构上不可能调 enqueue、模型或工具。
- 恢复路径唯一：`DB state → dispatcher → Python`。
- 退出码：0 成功 · 1 出错或指定收据无法 requeue · 2 用法错误。
- 参数解析拆到 `src/cli/outbox-args.ts`，便于单测（导入入口会执行 `main()`）。

### 3.5 退避（裁决 3）——**未实现，报备**

保持固定 5s 间隔。**理由不是「别扭」，是会打破冻结的 Phase 10 验收**：
P10-5 断言「投递失败后，下一次 `dispatchOnce()` 必须投递成功」，而按
`(memory_attempts, updated_at)` 计算的下次可试时间会让该行在 15s 内不可选，
该断言必失败。要同时满足两者，只能把投递路径拆成两套语义（定时器带退避、
`dispatchOnce()`/`flush()` 不带），那比固定间隔更糟。

方案原文本身把 Phase 10 标注为「**推荐优化，不是阻塞项**」，且该痛点的实际后果
（runtime 短暂重启 → 消息被 park）已由 Phase 9 的人工 replay 覆盖。

---

## 4. ④ 两个一致性修正

### 4.1 Prompt 身份句

```text
- 旧：我是 SmartCS 智能客服助手，可以帮你查询订单、处理退款、创建工单和检索服务政策。
- 新：我是 SmartCS 智能客服助手，可以帮你处理订单、退款、工单和服务政策相关问题。
      + 这里说的只是服务范围。具体能做什么（只读查询还是包含业务写操作），
        以下面的能力段落为准——不要在这里承诺超出该段落的能力。
```

能力细节交给已有的动态工具面段落（`WRITE_SECTION` / `WRITE_DISABLED_SECTION`
由同一个开关渲染）。实测（writes off 渲染）：

```text
- 如果被问到你是什么，回答：我是 SmartCS 智能客服助手，可以帮你处理订单、退款、工单和服务政策相关问题。
- 这里说的只是服务范围。具体能做什么（只读查询还是包含业务写操作），以下面的能力段落为准——不要在这里承诺超出该段落的能力。

## 业务操作（WRITE）
本次部署**未启用**业务写操作（退款提交、创建工单）。
- 用户提出退款或建单时，如实说明当前无法直接办理，并引导用户联系人工客服，不要宣称已经处理。
- 查询类能力不受影响，正常使用。

=== 仍宣称写能力？ false
```

### 4.2 Outbox 投递口径

改为 **At-least-once delivery + idempotent consumer**：

- `pi-harness/src/session/outbox.ts` 模块注释：删掉「delivery happens exactly once」，
  写明崩溃意味着**再投一次**，重复由 CAS + 消费端幂等吸收，网络跳从不是 exactly-once。
- `docs/recovery.md` §3：原「下一个进程自然补投——**恰好一次**」改为完整口径说明。
- `pi-harness/tests/phase3-context-compliance.test.ts` P3-3 用例名同步。
- `pi-harness/README.md` Memory outbox 段新增投递契约小节。

**范围判据**（见 §8-D2）：只改「投递语义」这一类声明。保留两类：
① **反向边界声明**（"不承诺任意外部工具 exactly-once"）——它们本来就在说正确的话；
② **别的机制**的一次性语义（`agent_settled` 每 prompt 一次、ledger settle、收据孤儿改判、
RRF 候选顺序、tiktoken 缓存、approval CAS、Python 记忆租约）。

---

## 5. ⑤ 文档与 CI

- 根 README 首屏（Phase 3 核对微调）：标题 `# SmartCS`，一句话说明「一个仓库两套运行时」，
  两个入口直达；架构摘要新增「探针与运维入口」小节；项目结构树补 `pi-harness/`。
- 历史段落（Verified Local Status / Final RAG Runtime Closure / 用户登录与访问控制）
  按原有 `Historical Migration Record` 声明**保留原样**，只修路径。
- `.github/workflows/build-image.yml` 新增独立 `harness` job（Node 22 · `npm ci` ·
  `typecheck` · `test:ci`），Python job 原样保留；`build-and-publish` 依赖的仍是 Python job。
- `pi-harness/vitest.ci.config.ts`：**显式**列出 18 个离线套件并逐文件写明离线理由。
  成员资格是静态声明，不是运行时「失败就跳过」——CI 不假绿（见 §5.1）。

### 5.1 离线子集怎么证明的

不是靠「import 里没有 MySQL helper」推断，而是**把依赖打到不可达再跑**：

```bash
MYSQL_HOST=127.0.0.1 MYSQL_PORT=1 MYSQL_PASSWORD=offline-probe-invalid \
PYTHON_INTERNAL_BASE_URL=http://127.0.0.1:1 \
npx vitest run <18 files>
→ Test Files 18 passed (18) / Tests 95 passed (95)
```

同样的 18 个文件通过 `npm run test:ci` 复跑：`18 passed / 95 passed`。

**不在 CI 里、因而 CI 覆盖不到的**：需要真实库或真实 Python 的一切——crash matrix、
state/transport matrix、memory outbox 端到端链路、JWT interop，以及 Python 侧 pytest
（它有自己的 job）。本地「全绿」的口径仍是 `npm test` 全量。

---

## 6. ⑥ 本地形态收敛 —— 已执行（2026-10-05）

**门禁**：验收方首轮回复「暂缓」（用户验收状态未确认），第二轮明确放行「用户确认验收结束」。

### 6.1 收敛前的实测（留档）

```text
127.0.0.1:8971   PID 39396   native Node Harness
0.0.0.0:8001 / 127.0.0.1:3307 / 127.0.0.1:6379 / 127.0.0.1:8972   PID 28100  Docker (compose)
127.0.0.1:8000   PID 49984   native `python -m uvicorn api.main:app --port 8000`
```

关键发现：

- **Docker harness 本来就在跑**（`127.0.0.1:8972->8971`，healthy）——这正是方案 §12 描述的
  双实例现场，不是"计划中的收敛"。
- native harness 确认从 **sibling 目录**启动
  （`...\project005_SmartCS\pi-harness\src\server\main.ts`），与仓库副本无关。
- 两者**共用同一个 MySQL**（`127.0.0.1:3307` / `smartcs_checkpoint`），不是各自的库。
- 收敛前两实例均 healthy 且 `sessions.active=0`（无在途会话）；`agent_run_receipt` 18 行
  全部 `done`（无 pending/failed），**停实例不会丢任何投递**。
- `SMARTCS_PI_ROLLOUT_PERCENT=0`（api 容器）——新会话仍全走 legacy，存量 pi 会话按
  `harness_version` 钉住，不受影响。
- **`AUTH_JWT_SECRET` 在 `.env` 与 `.env.docker` 中并不相同**（`INTERNAL_SERVICE_JWT_SECRET`
  相同）。容器两者都读 `.env.docker`，所以任何手工签发的 user JWT 必须用 `.env.docker` 的密钥——
  用 `.env` 的会 401。不加说明的话，这是"冒烟假失败"的现成来源。
- **compose 不会接管 8000**：`smartcs-api` 只发布**宿主 8001** → 容器 8000。PID 49984 是宿主的
  独立进程，与 compose 无交集，全程未动。

### 6.2 收敛序列

1. **停 native 8971（PID 39396）**：`taskkill /PID` 被拒（"can only be terminated forcefully"），
   改用 `/F`。**因此 SIGTERM 优雅退出未执行**——按设计安全（outbox 与收据都从持久状态恢复，
   `kill -9` 本就是设计覆盖的窗口），且当时无在途会话、outbox 全 `done`。
2. **从 monorepo 路径重建 harness 镜像**：`docker compose build smartcs-pi-harness`
   （context `./pi-harness`）→ `smartcs-pi-harness:local`（1.01GB）。
3. **重建 api 镜像**：`docker build -t smartcs-api:phase11 .`。重层全部 `CACHED`（**未拉 torch**）；
   `smartcs-api:phase9` 保留为回滚点。
4. **`SMARTCS_IMAGE=smartcs-api:phase11 docker compose up -d smartcs-api smartcs-pi-harness`**
   → harness 回到 **8971**（仓库 compose 默认；api 侧 `PI_HARNESS_BASE_URL=http://smartcs-pi-harness:8971`
   是**容器内**端口，不受宿主映射影响）。

> 为什么必须一并重建 api：compose 里 api 用的是预构建镜像，实测
> `GET :8001/internal/ready → 404`。harness 的新 healthcheck 打 `/ready`，而 `/ready` 要调
> Python 的 `/internal/ready` —— 不同步重建 api，harness 容器会**永远 unhealthy**。
> 这是新探针引入的跨服务版本依赖，必须一起收口。

### 6.3 冒烟结果（全绿）

| 检查 | 结果 |
|---|---|
| 容器启动行自检 | `providerMode:openai writeMode:live skills:on knowledgeTransport:http` —— 4 个开关显式，无静默降级 |
| `GET :8971/health` | `{"ok":true,"sessions":{...}}`（轻量 liveness，未加重） |
| `GET :8971/ready` | **HTTP 200** `{"ready":true,"degraded":false,"warnings":[],"checks":{"mysql":{"ok":true},"business_runtime":{"ok":true},"mcp":{"enabled":false,"ok":true}}}` ← **新探针的首次生产验证** |
| `GET :8001/internal/ready`（无凭据） | **401**（鉴权生效，未裸奔） |
| `GET :8001/internal/ready`（service JWT） | 200 `{"ok":true,"platform_db":true,"tool_runtime":true,"memory_runtime":true}` |
| `GET :8971/internal/ops/memory-outbox`（service JWT） | 200，`durable{pending:0,failed:0,...}` + `dispatcher{passes,delivered,delivery_failures,parked,last_*}` |
| **一轮 pi chat**（统一入口 `POST :8001/api/chat`） | **HTTP 200（2.5s）**，真实模型回复，`harness_version:"pi"` |
| 持久链 | receipt #19 `completed` + `memory_enqueue_status='done'`；`memory_source_event` 1 行；outbox 19/19 `done`；`dispatcher.delivered=1` —— 新仪表**实时反映**了刚发生的这一轮 |
| `docker compose ps` | harness / api / redis / mysql **全部 healthy** |

### 6.4 收敛后形态

```text
127.0.0.1:8971   smartcs-pi-harness        (Docker，唯一 harness 实例)
0.0.0.0:8001     smartcs-api               (Docker，smartcs-api:phase11)
127.0.0.1:3307   smartcs-checkpoint-mysql
127.0.0.1:6379   smartcs-redis
127.0.0.1:8000   native uvicorn（未动，与 compose 无关）
```

实例唯一性证据：`8971` 的监听者变为 Docker 代理（PID 28100）；**`8972` 监听消失**；
按命令行匹配 `*pi-harness*src/server/main.ts*` 的 node 进程为空；PID 49984 仍在（StartTime 未变）。

---

## 7. 验收清单逐项证据（方案 Phase 14）

| # | 条目 | 结果 | 证据 |
|---|---|---|---|
| 1 | Git 根能看到 `pi-harness/` | ✅（git 动作 N/A-验收方） | `git ls-files pi-harness \| wc -l` = **110** |
| 2 | `pi-harness/.git` 未被嵌套 | ✅ | `ls pi-harness/.git` → No such file or directory |
| 3 | `node_modules`/`.runtime`/`.env` 未进 Git | ✅ | `git ls-files pi-harness` 三者皆无；`git check-ignore -v` 命中 `.gitignore:1,2` 与根 `.gitignore:2` |
| 4 | Compose 路径全部适配单仓库 | ✅ | §1 表；`context: ./pi-harness`、`./.env` 挂载、`.dockerignore` 排除 |
| 5 | 根 README 第一屏即最终 Pi 架构 | ✅ | §5；首屏即「一个仓库两套运行时」+ 双层图 |
| 6 | `npm ci` | ✅ | 仓库内 `pi-harness/` 执行，exit 0 |
| 7 | `npm run typecheck` | ✅ | `tsc --noEmit` 干净（含新 `vitest.ci.config.ts`） |
| 8 | Phase 10 Outbox 5/5 | ✅ | 单独复跑 `phase10-memory-outbox.test.ts` → **5 passed (5)**：P10-1 / P10-3 / P10-4 / P10-5 / P10-2（P10-2 为真实进程 `main.ts` + 崩溃点，9.4s） |
| 9 | Tool Surface 9/9 | ✅ | `phase10-tool-surface.test.ts` — 9 tests passed |
| 10 | MCP Transport 4/4 | ✅ | `phase8-mcp-transport.test.ts` — 4 tests passed |
| 11 | `/health` 仍是轻量 liveness | ✅ | 实现未动；P11-R10 断言其响应仍是 `{ok:true,...}` |
| 12 | MySQL 正常时 `/ready` 200 | ✅ | P11-R1（注入桩）+ 全量 vitest 中 `/ready` HTTP 用例 |
| 13 | MySQL 不可达时 `/ready` 503 | ✅ | P11-R2 |
| 14 | Python Runtime 不可达时 `/ready` 503 | ✅ | P11-R3 |
| 15 | HTTP 传输下不依赖 MCP | ✅ | P11-R4：无 MCP 配置仍 `ready:true`，`mcp:{enabled:false,ok:true}` |
| 16 | MCP 传输下 Gateway 不可达 → 503 | ✅ | P11-R5（端口 1）；反向用例 P11-R6/R6b（含 token 错误） |
| 17 | `/internal/ops/memory-outbox` 受内部认证保护 | ✅ | P11-O5：无凭据 / 乱码 token / 错 scheme 均 401 |
| 18 | 能看到 pending / failed / oldest age | ✅ | P11-O3（聚合）+ P11-O5b（接口体） |
| 19 | failed outbox 可人工重新置为 pending | ✅ | P11-O4（条件 UPDATE 语义）+ P11-O6（真 CLI 进程） |
| 20 | 人工 replay 不执行 LLM / Tool | ✅ | P11-O6：把 runtime URL 指向**记录型桩**，CLI 跑完后桩收到 **0 次**调用 |
| 21 | 文档不再宣称 exactly-once delivery | ✅ | §4.2；`docs/recovery.md`、`outbox.ts`、P3-3 用例名 |
| 22 | writes-off Prompt 无能力自相矛盾 | ✅ | §4.1 实测渲染 |
| 23 | 最终只运行一个 Pi Harness 实例 | ✅ | §6.3/§6.4：native 已停，8971 归 Docker；8972 监听消失；无 residual native node 进程 |
| 24 | GitHub Actions 至少验证 Python + Pi 两侧 | ✅（workflow 就位，未 push / 未远端运行） | `.github/workflows/build-image.yml` 两个 job |

### 新增测试

| 文件 | 用例数 | 依赖 | 说明 |
|---|---|---|---|
| `tests/phase11-readiness.test.ts` | 14 | 全离线 | 探针全部分支 + HTTP 面 |
| `tests/phase11-outbox-ops.test.ts` | 5 | 全离线 | dispatcher 计数 + CLI 参数 |
| `tests/phase11-outbox-ops-db.test.ts` | 6 | 真实 MySQL | 聚合 / replay / 接口认证 / 真 CLI 进程 |
| `tests/test_internal_ready.py` | 8 | 真实 MySQL | Python readiness 全部分支 |
| `tests/test_mcp_gateway.py`（追加） | 3 | 真实 gateway 子进程 | `/health` 已认证 / 未认证 |

---

## 8. 偏差与判断记录

### D1 · README 里的「Python 验收测试在 `tests/python/`」是事实错误，已一并修正

`AGENTS.md` 与 `pi-harness/README.md` 都写着 Python 侧验收入口是
`pi-harness/tests/python/test_internal_api_auth.py`。**该路径在两侧仓库都不存在**；
文件实际在 `tests/test_internal_api_auth.py`（自 `8e4c68e` 起就在 Python 树里，
`git log --all -- 'pi-harness/tests/python/*'` 无任何历史）。命令与表格行已改成
「从仓库根跑 `python -m pytest tests/test_internal_api_auth.py`」。

### D2 · exactly-once 的清理口径（§4.2）

按「投递语义」分类处理，未做全库文本替换。保留的反向边界声明与其它机制的一次性声明
清单见 §4.2；它们不是本轮 §11.2 的目标（该节开宗明义「不要宣称**网络投递** exactly-once」）。

### D3 · 工作区 `AGENTS.md` 未修改（**需你裁决**）

`D:\Workspace_for_Codex\project005_SmartCS\AGENTS.md` 在**仓库之外**（未被 Git 跟踪），
仍描述「两个 sibling 服务」与「在 D:\...\python-impl 做 Python 工作」。它现在与仓库形态
不符，会继续误导后续会话。但它在仓库外、不在本轮交付范围，故**未改**，在此标记。

### D4 · 未做，且按裁决属于「不做」清单

RAG 性能 / CrossEncoder / Recall·MRR·nDCG / Python→TS 全量迁移 / Subagent / 全量 MCP 化 /
多实例分布式 lease / 重写 WRITE·ExecutionLedger·PendingAction / 升级 Pi 1.0.1 —— 均未触碰。

### D5 · 新增了一个方案未列出的告警码

`/ready` 的 `warnings[]` 除 `memory_outbox_backlog`（`pending>0`，**触发 degraded**）外，
还会在 `failed>0` 时带 `memory_outbox_failed`（**不触发 degraded**）。理由：新做的运维面
既然暴露 parked 行，readiness 完全无视它就少了一个信号。属于加性改动，已在此报备。

### 裁决回执（验收方，2026-10-05）

| 项 | 裁决 | 备注 |
|---|---|---|
| D-A（`PYTHON_IMPL_ROOT` 自纠） | **认可** | 记录为「全量集成测试必须在仓库内真实跑」的例证：`tsc` 抓不到，只有真实 DB 测试能暴露 |
| §3.5 退避保持固定间隔 | **采纳兜底分支** | 打破冻结的 Phase 10 断言是不可接受的代价；方案原文自标"非阻塞"；人工 replay 已覆盖该痛点 |
| D3（工作区 `AGENTS.md`） | **登记为验收方待办** | 在 ⑥ 收敛完成后统一更新，届时一并决定 sibling 目录去留 |
| D5（额外 warning 码） | **追认合规** | — |
| §4.2 / D2 exactly-once 分类清理 | 认可 | — |
| D1 `tests/python/` 事实错误修正 | 认可 | — |
| ⑥ 本地形态收敛 | **放行并已执行** | 首轮「暂缓」（用户验收状态未确认）→ 次轮「用户确认验收结束」→ 见 §6 |

验收方同期进行 ①–⑤ 的**独立终验**（typecheck + `test:ci` + 全量 vitest + pytest），
**测试库窗口归验收方**；本会话在此期间不再发起任何 DB 套件运行。

### D6 · Phase 11 之前的报告保留原样

`PHASE0..10_REPORT.md` 是历史验收记录，其中路径仍是当时生效的 sibling 形态，**逐字保留**；
`pi-harness/README.md` 的历史段新增一句说明这一点。

---

## 9. 基线复核

全部在**仓库内**执行，且**串行**（同一个 `smartcs_phase1_test` 库，未与第二个 runner 并发；
纪律见 `pi-harness/tests/README.md`）。

```text
npm ci                                exit 0
npm run typecheck                     tsc --noEmit 干净
npm test                              35 files / 191 tests passed          (549.98s)
npm run test:ci                       18 files /  95 tests passed          ( 47.31s)

python -m pytest -q                                             652 passed, 37 skipped (473.59s)
SMARTCS_CHECKPOINT_MYSQL_TEST=1 \
SMARTCS_AUTH_MYSQL_TEST=1 python -m pytest -q                   687 passed,  2 skipped (530.32s)
```

两次 pytest 的对照（与 Phase 10 §11 记录对齐）：

| 口径 | Phase 10 记录 | 本轮 | 差异解释 |
|---|---|---|---|
| 默认（开关关闭） | 641 passed / 37 skipped（Phase 8 记录） | **652 passed / 37 skipped** | +11 = 本轮新增用例；37 skip 与基线一致 |
| 超集（两个 MySQL 开关全开） | 675 passed / 2 skipped / **1 failed**（收集 678） | **687 passed / 2 skipped / 0 failed**（收集 689） | +11 新增 +1 即那个既有失败已由验收方修正；收集数 678+11=689 ✔ |

即：**基线集合内零回归，唯一的历史失败已成绿**。

CI 的另一个 Python 门禁也复跑了（本轮动了 README 与仓库结构，值得验）：

```text
python -m scripts.check_repository_readiness
→ Repository readiness: PASS   (checks=10)
```

`python -m evals.runner --json` 未单独复跑：本轮未触碰路由 / 工具 / 上下文 / Eval 任何路径，
且它已被 CI 的 Python job 覆盖。

---

PHASE11_DONE COMPLETED
