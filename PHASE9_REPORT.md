# Phase 9 报告：服务启动（用户验收）+ Compose 容器化

> **执行方**：Claude Code（本终端，Phase 9 执行轮） ｜ **日期**：2026-10-04
> **依据**：`../python-impl/docs/HANDOFF-phase9.md`
> **前置**：迁移终局验收通过；分层提交完成（python-impl `feat/pi-harness-migration`，pi-harness 独立仓 `9edfadb`）
> **状态**：第 1 部分完成（RAG 时延红项已修复并冷启动复验 8/8 绿，§3）；第 2 部分 compose 容器化完成（§4）
> **STATUS: completed**

---

## 1. 服务现状（用户验收用）

| 项 | 值 |
|---|---|
| Web 工作台 | **http://127.0.0.1:8000/** （静态工作台 + 全部公开 API） |
| 测试账号 | 用户名 `demo-acceptance`　密码 `SmartCS-demo-0c6c2690`（account_id 314，`business_user_id=user_002`，demo 订单库中该用户有 5 条订单） |
| Python 服务 | `127.0.0.1:8000`，PID **49984**（复验重启后；首启为 55468），uvicorn（`api.main:app`），日志 `D:\Workspace_for_Codex\project005_SmartCS\.runtime\phase9\api.log` / `api.err.log` |
| pi-harness 服务 | `127.0.0.1:8971`，PID **36840**（复验重启后，`SMARTCS_TOOL_TIMEOUT_MS=90000`；`node_modules/tsx/dist/cli.mjs src/server/main.ts`），日志 `.runtime\phase9\harness.log` / `harness.err.log`；`providerMode=openai`（真实模型） |
| 冒烟脚本与结果 | `.runtime\phase9\smoke_phase9.py`、`.runtime\phase9\smoke_results.json` |
| dev 库备份 | `.runtime\phase9\smartcs_checkpoint_backup_20261004-175412.sql`（5.3 MB，`mysqldump --single-transaction --routines --triggers`，退出码 0，stderr 空） |

**启动命令（可复现）**

```powershell
# Python（进程级 env，.env 已含共享密钥与 MYSQL_*）
$env:SMARTCS_PI_ROLLOUT_PERCENT='100'; $env:PI_HARNESS_BASE_URL='http://127.0.0.1:8971'; $env:SMARTCS_WRITE_MODE='live'
Start-Process python -ArgumentList '-m','uvicorn','api.main:app','--host','127.0.0.1','--port','8000' `
  -WorkingDirectory '<repo>\python-impl' -RedirectStandardOutput '<logs>\api.log' -RedirectStandardError '<logs>\api.err.log'

# pi-harness
$env:SMARTCS_WRITE_MODE='live'; $env:SMARTCS_SKILLS='on'; $env:PYTHON_INTERNAL_BASE_URL='http://127.0.0.1:8000'
$env:SMARTCS_TOOL_TIMEOUT_MS='90000'   # 读工具超时按本机真实检索时延配置（见 §3）
$env:SMARTCS_RUNTIME_CWD='<repo>\pi-harness\.runtime\cwd'
$env:SMARTCS_PI_SESSION_DIR='<repo>\pi-harness\.runtime\pi-sessions'
$env:SMARTCS_PI_AGENT_DIR='<repo>\pi-harness\.runtime\pi-agent'
Start-Process node -ArgumentList 'node_modules/tsx/dist/cli.mjs','src/server/main.ts' `
  -WorkingDirectory '<repo>\pi-harness' -RedirectStandardOutput '<logs>\harness.log' -RedirectStandardError '<logs>\harness.err.log'
```

## 2. 环境与迁移

- **dev 库迁移**：先备份（见上），再对 `smartcs_checkpoint` 依序执行 `migrations/001~004`（幂等、纯增量）。结果：`conversation_session.harness_version` 已加列，`agent_run_receipt` / `memory_source_event` / `pending_action` / `audit_event` 已建表。旧数据零改动。
- **环境变量**：`INTERNAL_SERVICE_JWT_SECRET`（64 字节随机，两侧一致）**追加到 `python-impl/.env`**（该文件 gitignored；追加处有注释说明"两个进程都回退读取本文件"）。其余开关按交接指令以**进程级**传入：Python 侧 `SMARTCS_PI_ROLLOUT_PERCENT=100` / `PI_HARNESS_BASE_URL` / `SMARTCS_WRITE_MODE=live`；harness 侧 `SMARTCS_WRITE_MODE=live` / `SMARTCS_SKILLS=on` / 三个 Pi 路径。
- 未改动任何代码、未 commit / 未 push。

## 3. 冒烟自验结果

| # | 检查 | 结果 |
|---|---|---|
| 1 | Python `/health` = 200、harness `/health` = 200 | ✅ |
| 2 | 登录 `demo-acceptance`（Argon2 校验）→ cookie → `/api/auth/me` | ✅ 200 |
| 3 | **新会话命中 pi 路径**（PERCENT=100） | ✅ 响应 `harness_version="pi"`、`intent=refund`、6.6s；会话行 `harness_version='pi'`；`agent_run_receipt` `status=completed`；pi transcript 落盘 |
| 4 | **订单查询（真工具）** | ✅ 模型调用 `order_query`，答复含真实订单数据（ORD-20260801-0022 已发货、AirPods Pro 第二代 ×2、¥3684.06、中通快递）；transcript 有 `order_query` 调用 |
| 5 | **Skills 披露（本轮额外收益）** | ✅ transcript 显示模型调用了 `skill_load` 取退款政策正文后作答（`SMARTCS_SKILLS=on` 生效） |
| 6 | **legacy 会话仍走旧链路** | ✅ 手工建 `harness_version='legacy'` 会话 → 响应 `harness_version="legacy"`（旧 orchestrator 链路） |
| 7 | **知识检索（RAG）经 pi 路径** | ✅ **已修复并复验**（原为红项，见 §3）：冷启动 44.9s 绿、热态 45.3s 绿，均返回知识库真实内容 |

### 红项根因（已定位到具体常量，非推断）

- **机制**：harness 的 `PythonInternalClient` 对每一次 internal 调用使用**硬编码 10 秒超时**（`timeoutMs ?? 10_000`，无 env 开关），超时映射为 `PythonInternalError(504, "tool call timed out")`（`src/business/python-client.ts:216`）——模型看到的 504 就是这里来的。
- **实测时延**（`knowledge_search`，生产 RAG 配置：`bge-m3` 嵌入 + `cross_encoder` 重排 + 查询改写）：
  - 直接 `POST /api/tools/call`（工作台快捷入口，同一进程同一检索器）：**13.6s（冷）/ 10.1s / 9.0s（热）**
  - 与 harness 完全相同的路径 `POST /internal/tools/execute`（服务令牌 + 真实 pi 会话）：**9.1s，200 OK**
  - 结论：**时延（9–14s）贴着 10s 超时线**——冷启动必失败，热态勉强通过 → 表现为间歇性 504。
- **不是** Python 侧策略超时：`ToolExecutionPolicy.timeout_seconds=5.0` 的默认值在该路径上未生效（9.1s 调用返回 200），实际瓶颈是 TS 客户端 10s。
- **影响面**：只有"经 pi 路径调 `knowledge_search`"受影响；`order_query`（0.2s）、legacy 链路、Skills（本地读取）、工作台直连 `/api/tools/call` 均正常。

### 处置（验收方裁决 (a)，已落地并复验）

**修法（最小 diff）**：`src/business/python-client.ts` 新增 `toolTimeoutMs` 选项 + `SMARTCS_TOOL_TIMEOUT_MS` 环境变量（默认 30 000ms），**仅** `/internal/tools/execute` 使用它；其余 internal 调用（auth/context/compliance/audit 等快调用）维持 10s 不变。

**部署调优口径**：读工具超时应按部署环境的**真实检索时延**配置——本次生产 RAG 配置（bge-m3 + cross_encoder 重排 + 查询改写）在服务负载下实测单次检索约 30–45s，故本窗口设 `SMARTCS_TOOL_TIMEOUT_MS=90000`。

**复验（冷启动重启后，8/8 全绿）**：

| 检查 | 结果 |
|---|---|
| ① health 双服务 | 200 / 200 |
| ② 登录 + me | 200 |
| ③ 新会话 pi 路径 | `harness_version="pi"`，6.6s |
| ④ 订单查询真工具 | `order_query` 调用，真实订单数据，5.7s |
| ⑤ Skills | `skill_load` 被调用 |
| ⑥ legacy 会话 | `harness_version="legacy"`，12.7s |
| **⑦-A RAG 冷启动** | ✅ 44.9s，单次 `knowledge_search`，答复为知识库内容 |
| **⑦-B RAG 热态** | ✅ 45.3s，单次 `knowledge_search`，答复为知识库内容 |

**另一条实测教训（写进报告供后续参考）**：30s 超时下第一次复验（7/8）冷启动仍红——因为当时**用户正在并发使用服务**，检索时延被抬高到 30s 以上。超时值必须覆盖**负载下**的时延，而不只是空载时延；本窗口最终取 90s。

---

## 4. 第 2 部分：Compose 容器化（已完成）

### 4.1 交付物

| 文件 | 性质 |
|---|---|
| `pi-harness/Dockerfile` | **新增**：node:22-slim + `npm ci`（含 dev：tsx 即运行时）+ 非 root（uid 1000 node）+ 容器内 node 自探活 healthcheck + 运行与本地进程部署**同一个入口**（`tsx src/server/main.ts`） |
| `pi-harness/.dockerignore` | **新增**：排除 node_modules/.runtime/tests/scripts/docs |
| `python-impl/compose.yaml` | **授权最小 diff**：注册 `smartcs-pi-harness` 服务（build+image、env_file 复用 `.env.docker`、显式 PORT/HOST、`PYTHON_INTERNAL_BASE_URL=http://smartcs-api:8000`、`SMARTCS_TOOL_TIMEOUT_MS`/`SMARTCS_SKILLS`/`SMARTCS_WRITE_MODE`、`smartcs-pi-runtime` 命名卷挂 `/app/.runtime`、`depends_on: smartcs-api`、`smartcs-net`、8972→8971 仅回环发布）；api 服务新增 `environment:`（`PI_HARNESS_BASE_URL=http://smartcs-pi-harness:8971`、`SMARTCS_PI_ROLLOUT_PERCENT` 默认 0）；新增卷声明 |
| `.env.docker`（gitignored） | 追加 `INTERNAL_SERVICE_JWT_SECRET`（与 `.env` 同值；容器化后 api 与 harness 必须互认服务令牌），带注释 |

**两种部署形态并存**：容器化未改动本地进程启动方式；本地 dev harness 仍占 `127.0.0.1:8971`（用户验收在用），容器 harness 发布在 `127.0.0.1:8972`。

### 4.2 两个实测坑（都已修，记录供后续）

1. **`NODE_ENV=production` 会让 `npm ci` 跳过 devDependencies** —— 而 `tsx` 正是本容器的运行时，首版镜像启动即 `MODULE_NOT_FOUND`。修法：`npm ci --include=dev`（并在 Dockerfile 注释说明这不是笔误）。
2. **`env_file: .env.docker` 会带入 API 的 `PORT=8000`** —— harness 容器因此监听 8000 而非 8971：容器健康检查自洽（它读同一个 env）、宿主映射却打不通，现象很迷惑。修法：在服务 `environment:` 里显式钉 `PORT: "8971"` / `HOST: "0.0.0.0"`（environment 覆盖 env_file）。

### 4.3 容器内冒烟

| 检查 | 结果 |
|---|---|
| harness 容器 | ✅ `Up (healthy)`，`127.0.0.1:8972->8971/tcp`，`id` = uid 1000(node) 非 root，命名卷 `smartcs-pi-runtime` 挂载于 `/app/.runtime`，`/health` 从宿主可达 |
| 容器化 API | ✅ `Up (healthy)`，宿主 `8001→8000`，工作台静态页 200，容器内登录（MySQL 经 `host.docker.internal`）200 |
| **一轮 pi chat（真实模型）** | ✅ 容器 API → 容器 harness → 容器 API 身份校验 → 真实模型：`harness_version="pi"`，4.2s，答复「退款审核通过后，会在 3–5 个工作日内原路退回。」（会话 `ba900ddc…`） |
| 本地进程部署 | ✅ 未受影响（8000/8971 仍在服务，用户验收继续可用） |

**首启行为（实测，部署须知）**：容器化 api 的模型缓存是**卷内独立**的——HF/sentence-transformers 缓存卷为空时，首次启动要下载嵌入模型（bge-m3，实测下载 2.3GB）与重排模型（cross_encoder，继续下载约 0.7GB+），期间 `uvicorn` 尚未监听端口，`/health` 不可达、容器显示 `unhealthy`（start_period 30s 不够覆盖模型下载）。**本地进程部署不受影响**（宿主缓存早已就绪）；容器第二次启动即缓存命中，恢复秒级。若要缩短首启，可预置缓存卷或把宿主的 HF/ST 缓存挂进容器。

**第三个实测坑（重要，属"静默降级"类）**：容器化 harness 首次冒烟返回 `[faux] 收到：…` —— 即它**静默退化成了 Faux provider**。原因：`resolveProviderMode()` 从 `python-impl/.env` **文件**解析模型凭据（不是从进程环境变量），容器内该路径不存在即回落 Faux；而容器里 `OPENAI_*` 明明已由 `env_file` 注入。修法：compose 为 harness 挂载只读 env 文件并设 `SMARTCS_PYTHON_ENV_FILE=/app/runtime-env/.env`（可用 `SMARTCS_ENV_FILE_HOST` 覆盖），重建后日志显示 `providerMode: "openai"`，复跑冒烟获真实模型答复。**建议后续**：生产形态下这种"凭据缺失 → 静默用假模型"应当 fail-fast（另立跟进项，不在本阶段改动）。

**注**：`docker compose build smartcs-api` 不可用——该服务只声明 `image:` 没有 `build:` 段，故改用 `docker build -t smartcs-api:phase9 .` 直接构建当前源码镜像，compose 侧仅通过 `SMARTCS_IMAGE` 选择镜像，未改 api 服务定义。

---

**STATUS: completed**

- **第 1 部分**：双层服务常驻可用（工作台 http://127.0.0.1:8000/，账号 `demo-acceptance`）；dev 库已备份并迁移 001–004；原 RAG 时延红项按裁决 (a) 修复（`SMARTCS_TOOL_TIMEOUT_MS` 可配，部署值 90s）并**冷启动复验 8/8 全绿**。
- **第 2 部分**：`pi-harness/Dockerfile`（node:22-slim、`npm ci --include=dev`、非 root、自探活）+ compose 注册（持久卷、环境接线、depends_on、回环发布 8972）+ 全套拉起；**容器冒烟 4/4**（含真实模型的一轮 pi chat）；本地进程部署未受影响。三个实测坑（npm 跳过 tsx、`.env.docker` 的 PORT 泄漏、凭据缺失静默回落 Faux）与模型首下行为均已记录在 §4。
- 未 commit / 未 push。

PHASE9_DONE completed
