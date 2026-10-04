# Phase 0 报告：版本审计 + Pi Runtime Spike + A1–A8 SDK 断言验证

> **执行方**：ds-for-act（本终端） ｜ **日期**：2026-10-03
> **依据**：`../python-impl/docs/HANDOFF-phase0.md`（交接指令） + `../python-impl/docs/pi-replatform-plan-v2.md`（权威计划 v2）
> **范围**：计划 v2 §3 版本审计 + §10 Phase 0。**未开始 Phase 1 及之后任何阶段**，未改动 `python-impl/` 任何文件，未 commit / 未 push。
> **STATUS: completed**

---

## 0. 结论速览

| 项 | 结论 |
|---|---|
| 版本冻结 | `@earendil-works/pi-coding-agent` **1.0.1**（npm latest 复核一致），lockfile 锁死 |
| A1–A8 | **8/8 全部 pass**，均有可重复运行的代码级测试 |
| Phase 0 验收清单 | 6/6 通过（loop / session 恢复 / tool_call hook / 无内置编码工具 / SSE 生命周期 / abort） |
| Provider 验证 | **未被阻塞**：真实 Moonshot OpenAI 兼容端点冒烟通过（含真实模型工具调用） |
| 测试 | **46 passed / 0 failed**（12 个测试文件），`tsc --noEmit` 干净 |
| 与计划偏差 | 发现 **12 项 API delta**，其中 **3 项影响计划正文**（§5.2 持久化语义、§5.2 无映射表假设、§3 漂移说明）。**未静默改计划**，全部记录于 §6 |

---

## 1. 版本审计（计划 §3）

### 1.1 latest 复核与冻结

```
$ npm view @earendil-works/pi-coding-agent version
1.0.1
$ npm view @earendil-works/pi-coding-agent dist-tags --json
{ "latest": "1.0.1", "legacy-node20": "0.74.2" }
```

复核结果与计划 §3「当前事实（2026-10-03 核查）npm latest = 1.0.1」**一致**。

**冻结版本：`1.0.1`（exact，非 `^` 范围）**

| 直接依赖 | 锁定版本 |
|---|---|
| `@earendil-works/pi-coding-agent` | 1.0.1 |
| `@earendil-works/pi-ai` | 1.0.1 |
| `@earendil-works/pi-agent-core` | 1.0.1 |

> `pi-ai` / `pi-agent-core` 由本 spike 直接 import（`Type`、`AgentMessage`、faux provider），因此显式声明为直接依赖并锁同版本 —— 避免依赖 npm 提升传递依赖这一隐性契约。

### 1.2 lockfile 摘要（计划 §3 第 3 步）

| 项 | 值 |
|---|---|
| 文件 | `pi-harness/package-lock.json` |
| 大小 | **109,156 bytes** |
| SHA-256 | **`d38f32b8c1be5607f69ff4d8ba680dc3924f84de22530c966d3e8cebcb9eb007`** |
| lockfileVersion | 3 |

1.0.1 起包方移除了 `npm-shrinkwrap.json`（CHANGELOG 已确认），传递依赖不再被包方锁定 —— 因此**自有 lockfile 是必须项**，本次已生成并纳入交付物。

### 1.3 目标版本 CHANGELOG 审阅（1.0.0 / 1.0.1）

已通读 `node_modules/@earendil-works/pi-coding-agent/CHANGELOG.md` 的 1.0.0、1.0.1 两节。计划 §3 引用的三条事实**全部核实为真**：

| 计划 §3 陈述 | CHANGELOG 核实 |
|---|---|
| 修复 provider "at capacity" 误杀 turn | ✅ Fixed: *"Selected model is at capacity" provider errors ending the turn instead of being retried (#10278)* |
| 修复 brace-expansion 安全漏洞 | ✅ Fixed: 固定 `brace-expansion` 5.0.12（GHSA-q2hr-2g5m-vwhr 等三项） |
| 移除包内 npm-shrinkwrap | ✅ Removed: *npm-shrinkwrap.json from the published package (#5653)* |

无 1.0.x 内的 breaking change 声明。

### 1.4 运行环境

| 项 | 值 |
|---|---|
| Node | **v24.14.1**（满足包 engines 要求；`legacy-node20` dist-tag 存在但未使用） |
| npm | 11.11.0 |
| 平台 | Windows 11 (win32)，Git Bash + PowerShell |

> **计划 §3 第 4 步（`dg-piagent` skill 基线 0.83.0 → 1.0.1）未执行**：该步骤属 skill 维护流程，其自身规程要求「报用户确认后再改」，且会修改 `~/.claude/skills/` 下的全局 skill（超出「产出留在工作区由验收方审查」的边界）。**列为待办**，见 §8。本次编码全程以目标版本 `node_modules` 的 `dist/**/*.d.ts` + `examples/sdk` + CHANGELOG 为准（交接指令 §2 规定的兜底协议）。

---

## 2. A1–A8 SDK 断言结论表

全部断言均以**可重复运行的最小测试**取证，测试位于 `pi-harness/tests/`。
运行命令：`cd pi-harness && npx vitest run`

| # | 断言 | 结论 | 证据测试 | 关键输出 |
|---|---|---|---|---|
| **A1** | `SessionManager.create(cwd, dir, {id})` 接受调用方指定 id；session id → 文件路径可推导 | **PASS**（含 1 项设计约束） | `tests/a1-session-id.test.ts`（5 tests） | id 原样成为 `getSessionId()` / header.id；文件名 `<ISO时间戳>_<id>.jsonl`；**路径不可由 id 纯推导**（时间戳前缀是创建时刻），SDK 提供 `SessionManager.findById(cwd, id, dir)` 做精确查找 |
| **A2** | `message_end` 扩展可返回 replacement message | **PASS** | `tests/a2-message-end-replacement.test.ts`（2 tests） | `replacementCount === 1`；替换后的文本同时出现在 ① 内存 session state ② `buildSessionContext()` ③ JSONL 落盘；原始泄漏串不出现在 assistant entry 中 |
| **A3** | 事件顺序：extension `message_end` → public listeners → `appendMessage` | **PASS** | `tests/a3-event-order.test.ts`（1 test） | 扩展观察点索引 < listener 索引；两层观察到的「已持久化 message 数」相等，且 prompt 结束后 = 该值 + 1（即 append 发生在两者之后） |
| **A4** | file-backed append 落盘时机（durability point 是否成立） | **PASS（带偏差）** | `tests/a4-durability.test.ts`（5 tests） | 首个 user/assistant 消息在 `appendMessage()` 返回时**已同步落盘**；但**setup-only entry（model_change / thinking_level / custom）不落盘**，文件在首个会话消息前根本不存在 → 计划 §5.2 的表述需收窄，见 §6-D1 |
| **A5** | 同 session id 被两个进程打开的行为 | **PASS（含 2 项风险）** | `tests/a5-two-processes.test.ts`（4 tests）+ `tests/fixtures/session-worker.mjs` | SessionManager **无跨进程锁**（`proper-lockfile` 仅用于 auth-storage / settings-manager）；顺序同 id 两次 create → **两个文件共享同一 id**（split-brain）；同毫秒 create → 第二个写入者 `openSync(..., "wx")` **抛 EEXIST**；第二进程 `open()` 现有文件后追加**不丢历史** |
| **A6** | `agent_settled` 每 prompt 恰好一次（含 retry / compaction） | **PASS** | `tests/a6-agent-settled.test.ts`（5 tests） | 普通 prompt = 1；多轮 tool loop（`message_end` 触发 4 次）= 1；**真实自动重试**（`agent_start ≥ 2`）= 1；**真实 compaction**（断言 entries 含 `compaction` entry 且 `callCount ≥ 3`）= 1；连续两 prompt 各 = 1 |
| **A7** | reopen 同一 session 恢复 active branch | **PASS（含 1 项风险）** | `tests/a7-reopen-session.test.ts`（3 tests） | 按路径 `SessionManager.open()` 与按 `findById()` 查找均恢复出相同 id / leafId / entry 数 / 模型上下文；branch 后 reopen 恢复到**分支 leaf**，被放弃路径**不进入**模型上下文；**风险**：对不存在的路径 `open()` 会静默新建一个空 session（不抛错） |
| **A8** | `defaultTools` / 白名单在 server 模式下确实禁用内置工具 | **PASS（强于预期）** | `tests/a8-tool-whitelist.test.ts`（5 tests） | `getActiveToolNames()` = 仅 `["order_query","knowledge_search"]`；`getAllTools()` **完全不含** read/bash/edit/write/grep/find/ls（不是「注册但未激活」，而是**根本未注册**）；`before_agent_start.systemPrompt` 只宣传这 2 个工具；模型强行调用 `bash` 时 SDK 返回 `isError:true, "Tool bash not found"`，扩展审计 hook **未观察到任何 bash 执行**；`noTools:"builtin"` 变体亦通过 |

**断言失败 / 不存在 API：无。** 8 项全部成立，无需回炉计划 §5.2 / §6.5 的承重墙设计。

---

## 3. Phase 0 验收清单逐项结果

计划 §10 Phase 0 验收：*agent loop 可跑；session 可外部恢复；tool_call hook 正常；无 bash/read/write/edit；SSE 生命周期正常。*

| 验收项 | 结果 | 证据 |
|---|---|---|
| agent loop 可跑 | ✅ | `tests/phase0-acceptance.test.ts`（8 tests）；`tests/smoke.test.ts` |
| session 可外部恢复 | ✅ | `tests/a7-reopen-session.test.ts`（3 tests）；`src/spike/run-spike.ts` 的 `reopen` 段（进程内 dispose 后重开，entryCount=7、上下文角色序列一致） |
| tool_call hook 正常 | ✅ | `tests/a8-tool-whitelist.test.ts` 端到端工具调用；`tests/phase0-acceptance.test.ts` 证明 `tool_call`/`tool_result` 为**扩展独有**（`session.subscribe` 收不到且不报错，与计划 §8 #7 一致） |
| 无 bash/read/write/edit | ✅ | `tests/a8-tool-whitelist.test.ts`（工具注册表层面剔除，非仅停用） |
| SSE 生命周期正常 | ✅ | 真实 HTTP 验证，见 §3.2 |
| abort | ✅ | `tests/phase0-acceptance.test.ts` 中止用例：`settledCount===1`、`done.reason==="aborted"`；`src/spike/run-sse-server.ts` 的 `POST /abort` 与 SSE 断连自动 abort |

### 3.1 交付物结构（计划 §4.2，按交接指令「允许最小化」）

```text
pi-harness/                     ← 与 python-impl 平级；位于 git 仓库之外
├─ package.json / package-lock.json / tsconfig.json / vitest.config.ts
├─ src/
│  ├─ config/env.ts              ← 显式路径 + 从 python-impl/.env 只读加载 LLM 配置
│  ├─ agent/
│  │  ├─ create-smartcs-agent.ts ← 组装入口（provider / loader / tools / session）
│  │  ├─ prompt/customer-service.ts
│  │  ├─ tools/fake-tools.ts     ← 2 个 fake 只读工具（硬编码，不调 Python）
│  │  └─ extensions/{audit,compliance}.ts
│  ├─ streaming/{status,chat-stream}.ts   ← status 通道 / final buffer / SSE 帧
│  └─ spike/{run-spike,run-sse-server,run-provider-probe}.ts
└─ tests/  (A1–A8 断言 + 验收 + plan-delta + helpers/ + fixtures/)
```

对照计划 §4.2 的目标结构：`agent/`、`streaming/` 已就位；`session/`、`business/`、`tracing/`、`server/` 属 Phase 1/2/6 范围（SessionRegistry、python-client、OTel、FastAPI 级 server），Phase 0 未建 —— 符合「只做 Phase 0」的硬约束。

### 3.2 SSE 生命周期实测（真实 HTTP）

```
$ SMARTCS_PHASE0_PROVIDER=faux PORT=8971 npx tsx src/spike/run-sse-server.ts
$ curl -s http://127.0.0.1:8971/health
{"ok":true,"providerMode":"faux","sessionId":"smartcs-phase0-sse"}

$ curl -s -N "http://127.0.0.1:8971/chat/stream?q=帮我查订单1001"
event: status
data: {"type":"status","text":"正在查询订单","at":1791036278031}

event: final
data: {"type":"final","text":"你的订单已发货。","at":1791036278040}

event: done
data: {"type":"done","at":1791036278040,"reason":"settled"}
```

status 文本由工具名确定性映射（`statusForTool`），**模型 narration 不进入 status 通道**（计划 §6.5 约束已落实到代码）。

### 3.3 Provider 验证（未被阻塞）

`.env` 实际配置与计划 §1/附录B 存在出入（见 §6-D2），但**端点可用**，验证未阻塞：

```
$ npx tsx src/spike/run-provider-probe.ts
{"event":"config","baseUrl":"https://api.moonshot.cn/v1","model":"kimi-k2.7-code",
 "apiKeyPresent":true,"sourceFile":"...python-impl\\.env"}
{"event":"provider_probe","status":"ok","latencyMs":1320,"settledCount":1,
 "finalText":"收到。","stopReason":"stop","provider":"smartcs-openai-compat",
 "model":"kimi-k2.7-code","usage":{"input":244,"output":18,"cacheRead":256,...}}

{"event":"tool_probe","status":"ok","latencyMs":3253,
 "calledTools":["order_query"],"toolResultCount":1,"toolResultIsError":[false],
 "finalText":"订单 1001 当前状态为：**已发货**。\n\n物流信息：\n- 承运商：顺丰速运..."}
```

真实模型（`kimi-k2.7-code`，`openai-completions` + compat）**自主选择了白名单内的 `order_query`**，拿到 `[FAKE]` 数据并生成最终答复；全程未触碰任何编码内置工具，未调用 Python。

**compat 配置**（计划 §附录A「按需」项，本次显式固定，不依赖 URL 自动探测）：

```ts
{ supportsDeveloperRole: false, supportsStore: false,
  maxTokensField: "max_tokens", supportsReasoningEffort: false }
```

---

## 4. 测试运行命令与最终结果

### 4.1 命令

```bash
# 安装（锁定 1.0.1）
cd D:/Workspace_for_Codex/project005_SmartCS/pi-harness
npm install

# 全量测试
npx vitest run

# 类型检查
npx tsc --noEmit

# 端到端 spike（离线 faux）
SMARTCS_PHASE0_PROVIDER=faux npx tsx src/spike/run-spike.ts "帮我查一下订单 1001"

# 真实 provider 探针（读 python-impl/.env，只做只读推理）
npx tsx src/spike/run-provider-probe.ts

# SSE 服务
SMARTCS_PHASE0_PROVIDER=faux PORT=8971 npx tsx src/spike/run-sse-server.ts
curl -N "http://127.0.0.1:8971/chat/stream?q=帮我查订单1001"
```

### 4.2 最终结果

```
$ npx vitest run
 Test Files  12 passed (12)
      Tests  46 passed (46)
   Duration  33.56s

$ npx tsc --noEmit
(无输出 = 干净)
```

| 测试文件 | 用例数 | 覆盖 |
|---|---|---|
| `tests/a1-session-id.test.ts` | 5 | A1 |
| `tests/a2-message-end-replacement.test.ts` | 2 | A2 |
| `tests/a3-event-order.test.ts` | 1 | A3 |
| `tests/a4-durability.test.ts` | 5 | A4 |
| `tests/a5-two-processes.test.ts` | 4 | A5 |
| `tests/a6-agent-settled.test.ts` | 5 | A6 |
| `tests/a7-reopen-session.test.ts` | 3 | A7 |
| `tests/a8-tool-whitelist.test.ts` | 5 | A8 |
| `tests/a8b-provider-events.test.ts` | 1 | provider 事件可达性（delta） |
| `tests/plan-deltas.test.ts` | 6 | §6 API delta 取证 |
| `tests/phase0-acceptance.test.ts` | 8 | 验收清单 |
| `tests/smoke.test.ts` | 1 | 组装冒烟 |

**失败项：无。** 全部用例可重复运行；`tests/a5-*` 使用 `tests/fixtures/session-worker.mjs` 启动真实子进程，其余为进程内测试。

> **测试可重复性说明**：所有离线测试使用 Faux provider，不发真实网络请求、不写 `python-impl/`、不产生业务副作用；每个用例使用独立临时目录与独立 faux 注册，互不串扰。`fileParallelism: false`（`vitest.config.ts`）以避免 A5 子进程探针受并发干扰。

---

## 5. 硬约束合规自查

| 约束 | 状态 |
|---|---|
| 不接真实 WRITE；fake 工具不调 Python 端点 | ✅ 2 个 fake 工具为纯硬编码；真实 provider 探针仅只读推理 |
| 不改 `python-impl/` 下任何现有代码（只读引用） | ✅ 全部写入均在 `pi-harness/`；仅**读取** `python-impl/.env`（`/api/tools/*` 从未调用） |
| 不开始 Phase 1 及之后阶段 | ✅ 未建 MySQL 表、未写 internal_api、未动 auth |
| 不 commit、不 push | ✅ `python-impl` HEAD 仍为 `abf71d2`，工作区无本次改动；`pi-harness/` 位于 git 仓库之外（repo root = `python-impl`），天然未被跟踪 |
| 测试必须可重复运行 + 命令入报告 | ✅ 见 §4.1 |
| 测试全绿或如实列出失败 | ✅ 46/46 passed，无失败项 |
| 计划与 SDK 冲突时如实记录、不静默改计划 | ✅ 见 §6，计划文件未做任何修改 |

---

## 6. 与计划 v2 的 API Delta 与偏差说明

> 全部结论以**冻结版 1.0.1 的 `dist/**/*.d.ts` + 源码 + `examples/sdk` + CHANGELOG** 为依据，并有 `tests/plan-deltas.test.ts` 等测试取证。

### 6.1 影响计划正文的偏差（需计划方裁决）

**D1 — 计划 §5.2 的 durability point 表述需收窄（重要）**

计划 §5.2 写：「**Durability point = 对应 SessionManager append 返回之后**（Pi 逐条 append JSONL）」。

实测（`tests/a4-durability.test.ts`）：

- 对 **user / assistant 消息**：成立。`_persist` 在首个会话消息时用 `openSync(path,"wx")` 同步写全部累积 entry，之后每条 `appendFileSync`，`appendMessage()` 返回时已在盘上。
- 对 **setup-only entry**（`appendModelChange` / `appendThinkingLevelChange` / `appendCustomEntry`）：**不成立**。`_persist` 有 `_hasConversation()` 守卫（`session-manager.js:791-814`），在会话出现 user/assistant 消息之前**根本不创建文件**。

**对计划的影响**：§5.2 / §6.3 / F1–F14 关于崩溃恢复的时序论证，必须限定为「**对话消息的 append**」。**Phase 1 若打算用 `appendCustomEntry` 在首轮 turn 开始前持久化业务快照，该写入不是 durable 的** —— 需改为在 user message 落盘之后再写，或由 Python 侧权威存储（本就如此设计，但时序上要说清）。

**D2 — 计划 §1 / 附录B 的 `MODEL_NAME` 与实际 `.env` 不一致（配置漂移）**

| 来源 | 值 |
|---|---|
| 计划 §1 / 附录B | `MODEL_NAME=deepseek-v4-flash` |
| `python-impl/.env` 实际 | `MODEL_NAME=kimi-k2.7-code`，`OPENAI_BASE_URL=https://api.moonshot.cn/v1` |

Provider 类型（OpenAI 兼容）判断不变，**但模型 ID 与端点不是 DeepSeek**。Phase 0 按交接指令「从现有 `.env` 读取」执行，验证通过（§3.3）。请计划方确认线上目标模型，并据此更新附录B 与 `compat` 假设（当前 compat 对 Moonshot 有效；换 DeepSeek 端点时 `maxTokensField` 等仍适用，但需复测）。

**D3 — 计划 §3「已知漂移提醒」中 session 版本为 v4 的说法不成立**

实测 `CURRENT_SESSION_VERSION === 3`（`tests/plan-deltas.test.ts`），落盘 header 为 `"version":3`。计划 §3 写「session 为 v4 lane-based」。**若 Phase 1 的迁移/兼容逻辑按 v4 设计将出错**；当前 SDK 自带 v1→v2→v3 自动迁移。

**D4 — 计划 §5.2「不维护映射表」的假设需要加一条约束（重要）**

`SessionManager.create(cwd, dir, {id})` 确实接受任意合法 id（A1 pass），但 **id 在目录内不唯一**：

- 顺序两次 `create(same id)` → **两个文件、同一个 id**（`tests/a5-two-processes.test.ts` 实测），`findById()` 只返回其中一个（readdir 顺序，属未定义）。
- id→路径**不是纯函数**（文件名含创建时刻时间戳），SDK 提供 `SessionManager.findById()` 做 O(n) 目录扫描查找。

**对计划的影响**：§5.2 的备选方案「若 session id → 文件路径不可推导，Phase 1 增加极薄 `pi_session_registry(session_id, file_path, created_at)`」。实测结论是**介于两者之间**：路径可通过 `findById` 找回，但**前提是同一 id 只被创建过一次**。建议 Phase 1 **二选一并写入计划**：(a) 维持无表 + 强制「先查后建」纪律（`findById` 命中则 `open`，未命中才 `create`），并加测试；(b) 直接采用 `pi_session_registry` 表做 O(1) 且强唯一。**仅靠 `create({id})` 不足以保证唯一性。**

**D5 — 计划 §6.5 可考虑新增的 `agent_before_settle` 钩子（正向 delta）**

1.0.1 新增扩展事件 `agent_before_settle`（`types.d.ts:763-766`）：*"Fired before final settlement. May append entries and ensure one next provider request."* 配合 `AgentBeforeSettleEventResult { continue?: boolean }`。

**对计划的影响**：§6.5 目前把 final 候选选择放在 `message_end`、发送点放在 `agent_settled`。`agent_before_settle` 提供了「结算前还能补一次持久化 / 再跑一次 provider 请求」的官方位置，Phase 3 设计合规与 final 缓冲时应评估是否需要它（例如：需要补齐一个缺失的 final 时）。

### 6.2 不改变计划结论、但实现时须知的 delta

**D6 — `extensionFactories` 不在 `createAgentSession` 上（计划附录A 示例不完整）**

计划附录A 的示例只写 `createAgentSession({ cwd, agentDir, sessionManager })`，随后用 `export default (pi) => {...}` 注册扩展，但**没有给出把扩展传给 session 的途径**。实测 `CreateAgentSessionOptions` **无** `extensionFactories` 字段；扩展必须经 `resourceLoader` 注入（`DefaultResourceLoader({ extensionFactories: [...] })`）。Phase 0 采用后者。

关键细节（已实测）：`noExtensions: true` **只关闭文件系统扫描**（`resourceLoader.js:403`），**不会**关闭 `extensionFactories`（`:244-245` 无条件保留）。因此「关闭 `~/.pi` 发现」与「注入自有扩展」可同时成立 —— 计划 §8 #4 的要求可实现。

**D7 — 内置工具是被「移除」而非「停用」**

计划 §8 #1 表述为 `defaultTools` / `builtin:<name>` 禁用项。实测：显式传 `tools: [...]` 白名单后，内置编码工具**根本不在工具注册表中**（`getAllTools()` 不含），模型若强行调用会得到 `isError: true, "Tool bash not found"`。`noTools: "builtin"` 亦可用（保留扩展/自定义工具）。**结论比计划更强**：白名单即等于「不存在」。

**D8 — `shouldStopAfterTurn` 已移除，`finishTurn` 存在（计划 §3 提醒正确）**

全库 grep `shouldStopAfterTurn` 无命中；`pi-agent-core/dist/types.d.ts` 中为 `finishTurn?: FinishTurn`。

**D9 — `message_update` 并非「只发 delta」（计划 §3 提醒已过时）**

实测 `assistantMessageEvent` 同时发出 `text_delta`（增量）与 `text_end`（`content` 字段为**已组装好的完整字符串**）。自行拼接 delta **不再是必需**；Phase 0 的 status 通道本就不消费模型文本。

**D10 — `session.agent.state.messages` 直接赋值确实「不生效」的准确含义（计划 §3 提醒正确）**

`session.agent` 是公开的 `readonly agent: Agent`，`agent.state` 返回活的 `_state` 对象，赋值**会**改内存状态；但 **SessionManager 才是 canonical** —— 转录与下一轮模型上下文都从 SessionManager 重建，因此该突变**是瞬时的、不影响 transcript**（`tests/plan-deltas.test.ts` 实测：抹掉 messages 后 `getEntries()` 不变，下一轮 `context` 事件仍带完整历史）。

**D11 — 1.0.1 新增一等公民「context edit」能力（正向 delta，利于 §6.8）**

SDK 提供 `ContextEditEntry` / `appendContextEdit(targetId, replacement)` / `buildSessionProjection()`：**append-only** 地替换或省略（`replacement: null`）早期 entry 对**模型上下文**的贡献，而**原始 entry 原样保留**（`tests/plan-deltas.test.ts` 实测：projection 看到替换后内容，`getEntries()` 仍是原始内容）。

**对计划的影响**：§6.8 的 history projector 设计要求「应用 context edit 的 replacement/omission」。这是 SDK 内建语义，**Python/TS 侧不必自行发明**该结构，直接用 `buildSessionProjection()` 即可。虽然不改变计划结论，但能显著简化 Phase 1 的 projector 实现。

**D12 — Provider 级事件在 Faux 下不可达（影响 Phase 6 测试策略）**

`before_provider_request` 依赖 provider 调用 `options.onPayload`；**Faux provider 从不调用 `onPayload`**（`pi-ai/dist/providers/faux.js` 仅调用 `onResponse`）。实测：`after_provider_response` 会触发，`before_provider_request` 恒为 0（`tests/a8b-provider-events.test.ts` 固化此行为）。真实 `openai-completions` 会调用 `onPayload`（`dist/api/openai-completions.js:188`）。

**对计划的影响**：§6.9 若要基于 `before_provider_request` 做请求级 trace / 审计，**该路径无法用 Faux 离线测试**，Phase 6 需为它单独设计真实 provider 或 mock provider 的测试方案。

### 6.3 其他实测观察（不构成计划偏差）

- **A2 的作用域**：`message_end` replacement 只覆盖 **assistant** 消息。用户在对话里说出的原文（含敏感串）仍会**原样落盘**。这与计划 §6.6「保留 raw user message provenance」一致，但需注意 §6.5「用户所见 == transcript 所存」**仅对 assistant 输出成立**。
- **`SessionManager.open()` 对不存在的路径静默新建空 session**（A7 第 3 个用例固化）。文件缺失（错误挂载卷、路径拼错）不会报错，而是得到一个**新 id** 的空会话。Phase 1 恢复路径必须自行校验文件存在性与 session id，不能依赖 `open()` 抛错。
- **Session 文件无跨进程锁**：`proper-lockfile` 仅用于 `auth-storage` / `settings-manager`。计划 §6.2「单实例 + per-session actor queue」是**唯一**的写者互斥保障，A5 的风险（同 id split-brain / EEXIST 崩溃）必须由 registry 或「先查后建」纪律在本进程外兜住。
- **session id 有字符集约束**：`assertValidSessionId` 要求 `^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$`（非空、仅字母数字与 `-_.`、首尾必须字母数字）。SmartCS 若用 UUID 形态的 session_id 可直接使用；若使用含 `:` 或其他字符的 id 会在 create 时抛错。

---

## 7. 交付物清单

| 交付物 | 路径 |
|---|---|
| Spike 代码 | `pi-harness/src/`（config / agent / streaming / spike） |
| 测试 | `pi-harness/tests/`（12 个测试文件 + helpers + 子进程 fixture） |
| 版本锁 | `pi-harness/package.json` + `package-lock.json`（sha256 见 §1.2） |
| 本报告 | `pi-harness/PHASE0_REPORT.md` |

---

## 8. 待办与移交

1. **计划方裁决 §6.1 的 D1–D4**（durability 表述、模型配置、session 版本、id 唯一性策略）—— 这四项影响计划正文，本次**未修改计划**。
2. **`dg-piagent` skill 基线升级**（计划 §3 第 4 步）：未执行，需按 skill 维护流程报用户确认后再改。
3. **Phase 1 前置**：建议先落定 D4 的唯一性策略，再设计 `SessionRegistry` + `agent_run_receipt`；否则 A5 的 split-brain 风险会直接进入崩溃恢复逻辑。

---

**STATUS: completed**

- 冻结版本：`@earendil-works/pi-coding-agent@1.0.1`（lockfile sha256 `d38f32b8c1be5607f69ff4d8ba680dc3924f84de22530c966d3e8cebcb9eb007`）
- A1–A8：**8/8 pass**，无失败、无阻塞
- 测试：**46 passed / 0 failed**（12 files），`tsc --noEmit` 干净
- Provider：真实端点验证通过（未被阻塞）
- 未 commit / 未 push / 未改 `python-impl/` / 未开始 Phase 1
