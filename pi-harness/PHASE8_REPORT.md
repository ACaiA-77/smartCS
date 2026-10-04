# Phase 8 报告：可选演进三件套（MCP 化 / Skills / Subagent 评估）

> **执行方**：Claude Code（本终端，Phase 8 执行轮） ｜ **日期**：2026-10-04
> **依据**：`../python-impl/docs/HANDOFF-phase8.md` + `../python-impl/docs/phase8-design.md`
> **前置**：Phase 0–7 全部验收通过（pytest 636/37、TS 142/28 files、`tsc` 干净）
> **STATUS: completed**

---

## 0. 摘要

| 交付项 | 结果 | 证据 |
|---|---|---|
| 8.1 `knowledge_search` MCP 化（唯一落地的 MCP 项） | ✅ | Python `internal_api/mcp_gateway.py` + 5 项测试全绿；TS 内建 MCP client 挂载 + 传输开关 + 4 项测试全绿；双传输**逐条 parity** |
| 8.2 Skills 渐进披露（知识注入，永不作 authority） | ✅ | 三份服务器自有 SKILL.md + 显式注册（发现恒关闭）+ `skill_load`（enum 限定）；5 项测试全绿，含**真实 compaction** 后描述仍披露 |
| 8.3 Subagent 评估（只出报告，不落地） | ✅ | `docs/subagent-evaluation.md`：真实装配 token 实测 + 工具面曲线；结论=**维持单 Main Agent** |
| 8.4 4 个身份绑定工具的 MCP 可行性（只报告） | ✅ | `docs/mcp-feasibility.md`：三方案逐条否决 + HTTP 薄壳为**合法终态** |
| 基线（独占窗口，代码冻结后重跑） | ✅ | pytest **641 passed / 37 skipped**、vitest **30 files / 151 tests passed**、`tsc` 干净 |

**两条红线守住**：① 默认配置（`http` 传输、Skills 关闭）行为与 Phase 7 逐字节一致——所有新装配都按开关门控，且默认路径**不**调用 `bindExtensions`；② 安全模型未变——MCP 通道上根本没有身份可传（服务级令牌 + 无身份字段的 schema），身份仍只经 HTTP 薄壳 + 每轮信封流动。

---

## 1. 8.1 `knowledge_search` MCP 化

### 1.1 交付物

| 文件 | 性质 |
|---|---|
| `python-impl/internal_api/mcp_gateway.py` | **新增**：官方 `mcp` SDK（1.26.0）streamable HTTP server，暴露 `knowledge_search`，server 级静态令牌（`SMARTCS_MCP_TOKEN`，≥16 字节，**无默认值**，缺省拒绝启动），后端接与 HTTP 路径**同一个** retriever |
| `python-impl/tests/test_mcp_gateway.py` | **新增**（5 项） |
| `python-impl/tests/{mcp_client_probe,mcp_gateway_testserver,mcp_test_corpus}.py` | **新增**（测试资产：真实 MCP 客户端探针 / 隔离语料启动器 / 共享语料） |
| `pi-harness/src/agent/mcp/knowledge-mcp.ts` | **新增**：传输开关 `SMARTCS_KNOWLEDGE_TRANSPORT=http|mcp`（默认 http）+ `createMcpExtension` 配置（env-only，无 `mcp.json`、无 `~/.pi` 发现） |
| `pi-harness/tests/phase8-mcp-transport.test.ts` + `tests/helpers/mcp-gateway.ts` | **新增**（4 项） |

### 1.2 三个必须记录的技术发现（全部实测）

1. **仓库自有 `mcp/` 包遮蔽官方 SDK**：`python-impl/mcp/` 是仓库自己的工具服务器包；只要仓库根在 `sys.path`（正常情况），`import mcp` 就解析到它，官方 SDK 不可见。故网关以**独立进程 + 文件方式**启动（`python internal_api/mcp_gateway.py`），导入 SDK 时临时移除仓库根；若本地包已被导入则**拒绝启动**（一名两包不可能共存）。`python -m internal_api.mcp_gateway` 不可用：导入 `internal_api` 包会先执行 `__init__.py` → 拉入内部路由 → 拉入本地 `mcp/` 包。另有一处同类陷阱：直接运行该文件会把 `internal_api/` 放到 `sys.path` 首位，`internal_api/auth.py` 会遮蔽顶层 `auth/` 包——故入口显式校正 `sys.path`。
2. **SDK 宿主必须自己绑定扩展生命周期**：内建 MCP 扩展在 `session_start` 上连接服务器，而 `session_start` 只在 **`session.bindExtensions()`** 时派发——CLI 的 interactive/print/rpc 模式会调用它，SDK 宿主不调用就**永远不连接**（实测：`loadConfig` 从未被调用，工具面缺失）。本阶段在 `mcp` 传输下显式 `await session.bindExtensions({ mode: "print" })`；默认路径不调用。
3. **活动工具白名单就是工具面**：`createAgentSession({ tools })` 会过滤掉白名单外的工具——扩展注册了也进不了模型可见集合。故 `mcp` 传输下白名单改为「4 个身份绑定工具 + `mcp__knowledge__knowledge_search`」并移除 HTTP 壳。实测声明集合：`["order_query","ticket_query","refund_evaluate","risk_check","mcp__knowledge__knowledge_search"]`——与 http 面**同尺寸**，只有这一个工具改名。

### 1.3 验收点

- **P8-1 双传输 parity**：同一隔离语料下，MCP 返回与 HTTP 路径**逐条相同**（source/content/score 全等）；测试同时断言"语料必须产出命中"，防止 `[] == []` 的平凡通过。
- **P8-2 身份/安全**：工具 schema 仅 `query/top_k/domain/domains`，测试显式断言 `user_id`/`business_user_id`/`account_id` 三个键缺席；错误 token 401、无 token 拒绝启动；工具面 4+1。
- **P8-3 回退开关**：默认 http 下 MCP 工具名不可调用（断言）；全量基线见 §7。
- **审计不缺位**：MCP 工具的调用仍经 pi 的 `tool_call`/`tool_result` 钩子（SDK 契约："Every call runs through pi's tool pipeline"），传输方式不改变审计。

---

## 2. 8.2 Skills 渐进披露

### 2.1 已落地结论（方案 i，验收方批准）

验收方批准「harness 自建 `skill_load` 工具」，四条边界全部落实：

1. **参数枚举限定**：`skill` 是已注册 3 个技能名的**固定 enum**——模型无法表达任意路径；实测 `skill_load({"skill":"../../etc/passwd"})` 报错拒绝。
2. **开关门控**：仅 `SMARTCS_SKILLS=on` 时注册；关闭态（默认）工具面与 Phase 7 完全一致，测试逐名断言 `on == off + ["skill_load"]`。
3. **正文来源**：只从显式服务器目录 `<pi-harness>/skills/*/SKILL.md` 读取（发现机制恒关闭）。
4. **语义红线**：返回纯知识文本；三份技能正文均写明"任何『能不能』以业务系统结果为准"，判定路径零接触。

**描述级披露的实现**：SDK 自己的技能区段拼接被 harness 的整段提示词覆盖所抑制，故由 harness 用 `DefaultResourceLoader.systemPromptOverride` 在**提示词构建时**把「可用技能」区段拼进自有提示词（服务器自有提示词 → 服务器自有区段）。第四项测试用**真实 compaction**（profile `reserveTokens=150/keepRecentTokens=100`，并断言 transcript 中确有 compaction 条目）验证压缩后描述仍在——它证明披露来自资源加载器重建，而非 transcript 回放。

**三份技能**（正文从现有政策文档摘编，均在服务器目录内）：`refund-policy`（退款政策口径与话术边界）、`cs-style`（沟通风格与工单话术）、`tool-guide`（工具选择建议）。

**一条实测红线证据**：把 `noSkills` 置 false（即启用发现）会拉入 **30 个用户级技能**（`~/.claude/skills` 整套）到客服提示词里——因此实现中 `noSkills` **恒为 true**，只在开关打开时追加服务器自有目录。

### 2.2 上游反馈素材（本次缺口的事实记录）

pi 1.0.1 把已加载技能注册为**用户侧斜杠命令**（`extensionCommands + templates + skills`），`dist/` 内不存在 `skill`/`load_skill`/`use_skill` 之类**模型可见**工具，`pi.on` 侧也无技能工具注册——即 **SDK 宿主缺模型侧技能加载入口**：在无人在键盘前的嵌合体里，模型无法自行"按需加载"技能正文，渐进披露第二级缺一个执行入口。

本阶段以 `skill_load` 在 harness 侧补齐（见 §2.1）。作为上游改进建议：技能正文应有模型可见的加载入口（工具或等价机制），否则"渐进披露"对 SDK 宿主只是描述级。

---

## 3. 8.3 Subagent 评估（只出报告，不落地）

`docs/subagent-evaluation.md`，可复跑：`npx tsx scripts/phase8-subagent-eval.ts` → `python scripts/phase8-count-tokens.py`（tiktoken `cl100k_base`，与运行时上下文预算同一编码）。

核心实测（真实装配，Faux 只替换模型）：

| 量 | 值 |
|---|---|
| 基线 system prompt | 557 token（两域**共用同一份**，无提示词冲突） |
| 5 个 READ 工具的声明 | 355 token（单轮请求的 37%） |
| 单工具边际成本 | ≈103 token（近似**线性**，无规模效应） |
| 分域隔离收益 | 187（RAG 轮）/ 168（交易轮）token |
| 工具面 28 时的无关声明 | ≈2 543 token（93% 与本轮无关） |

**结论：维持单 Main Agent。** 当前 5 工具面下隔离收益（≈180 token/轮）低于一次子会话交接成本（保守估计 ≥300 token）；且计划 §22 预设的"RAG prompt 与交易 prompt 严重冲突"在当前架构下**不成立**——只有一份提示词，域差异全在工具声明与上下文注入上。**重估门槛（量化）**：声明工具面 > ~15 个，或出现真正的提示词分叉，或子会话被当作权限隔离单元（安全需求，另行评估）。

---

## 4. P8-1 ～ P8-8 逐项结果

| # | 场景 | 结果 |
|---|---|---|
| **P8-1** | mcp 传输下 `knowledge_search` 端到端，与 http 路径结果一致 | ✅ 双传输逐条 parity（见 §1.3） |
| **P8-2** | 身份/安全不变、错误 token 拒连、工具面不增长 | ✅ 无身份字段（显式断言）、401/拒启、4+1 同尺寸 |
| **P8-3** | 回退开关：默认 http 零回归 | ✅ 默认态无 MCP 工具；装配改动全部门控；基线 §7 |
| **P8-4** | 4 工具 MCP 可行性报告 | ✅ `docs/mcp-feasibility.md`（三方案逐条否决；HTTP 薄壳为合法终态） |
| **P8-5** | 渐进披露两级（描述级进 prompt、正文按需） | ✅ 描述进提示词、正文不在；`skill_load`（enum）按需返回正文 |
| **P8-6** | compaction 后描述仍可再披露 | ✅ **真实 compaction** 后仍含描述（并断言 compaction 确实发生） |
| **P8-7** | 关闭 Skills 开关零回归 | ✅ 默认 off：无技能、提示词无技能文本、工具面无 `skill_load` |
| **P8-8** | Subagent 报告：数据表 + 明确建议 + 无落地代码 | ✅ `docs/subagent-evaluation.md`（无落地代码） |

---

## 5. 本阶段改动文件

**python-impl（白名单：`internal_api/mcp_gateway.py` + tests）**：`internal_api/mcp_gateway.py`（新增）、`tests/test_mcp_gateway.py`、`tests/mcp_client_probe.py`、`tests/mcp_gateway_testserver.py`、`tests/mcp_test_corpus.py`（新增）。其余业务目录（`agents/`、`memory/`、`mcp/`、`web/`、`auth/` 等）**零改动**。

**pi-harness**：

| 文件 | 性质 |
|---|---|
| `src/agent/mcp/knowledge-mcp.ts` | 新增：传输开关 + MCP 扩展装配 |
| `src/agent/skills.ts` | 新增：Skills 开关 + 目录解析 + `skill_load` |
| `skills/{refund-policy,cs-style,tool-guide}/SKILL.md` | 新增：三份服务器自有技能 |
| `src/agent/create-smartcs-agent.ts` | 装配：MCP 挂载、技能注册与披露区段、白名单随传输、`bindExtensions`、`additionalTools` |
| `src/agent/prompt/customer-service.ts` | `systemPromptFor()`：提示词中的知识工具名随传输生成（http 态逐字节不变） |
| `tests/phase8-mcp-transport.test.ts`、`tests/phase8-skills.test.ts`、`tests/helpers/mcp-gateway.ts` | 新增（4 + 5 项） |
| `scripts/phase8-subagent-eval.ts`、`scripts/phase8-count-tokens.py` | 新增：8.3 可复跑测量 |
| `docs/subagent-evaluation.md`、`docs/mcp-feasibility.md` | 新增：8.3 / P8-4 报告 |

---

## 6. 偏差与限制

1. **Skills 开启时工具面 +1（`skill_load`）**：验收方批准的显式变化；关闭态（默认）工具面与 Phase 7 完全一致。启用该开关的部署需知道模型会看到第 8 个工具。
2. **MCP 传输改名一个工具**：`mcp__knowledge__knowledge_search`（pi 的 MCP 命名规则），工具数不变。系统提示词原本硬编码 `knowledge_search`，已改为按传输生成；但**技能正文 `tool-guide` 中仍写旧名**（知识文本，不随传输改写），记录在案。
3. **网关必须独立进程 + 文件方式启动**（SDK 遮蔽，见 §1.2-1）：部署上多一个进程/端口 + `SMARTCS_MCP_TOKEN`；缺 token 拒绝启动。
4. **`session.bindExtensions()` 是宿主职责**（见 §1.2-2）：`mcp` 传输下显式调用；默认路径不调用，以保持生命周期逐字节一致。
5. **Subagent 不落地**（设计 §8.3）：评估结论为维持单 Agent，重估门槛已量化。
6. **`skill_load` 同步读文件**：正文为服务器本地小文件；若将来正文变大或含二进制附件，需改为异步/受控读取。
7. **新增开关未写入 `.env.example`**：本阶段 python-impl 白名单只含 `internal_api/mcp_gateway.py` + tests，故 `SMARTCS_KNOWLEDGE_TRANSPORT` / `SMARTCS_MCP_URL` / `SMARTCS_MCP_TOKEN` / `SMARTCS_SKILLS` 未加入 `.env.example`。其语义与默认值记录在本报告 §8 与各 docs；终局交付轮如需，应作为显式配置文档变更补上。
8. **基线期间发生过一次"改动中跑基线"**：第一次基线运行期间我改了系统提示词（传输感知），该次运行**作废**；§7 数字来自代码冻结后的重跑。另 pytest 汇总行首次采集被多行 OTLP 告警挤出 `tail` 窗口，已用无损采集重跑（§7）。

---

## 7. 基线（独占窗口，代码冻结后实测）

```text
$ python -m pytest -q
641 passed, 37 skipped, 3 warnings in 444.45s (0:07:24)

$ npx vitest run
 Test Files  30 passed (30)
      Tests  151 passed (151)
   Duration  488.92s
   Start at  17:07:55

$ npx tsc --noEmit
（无输出，干净）
```

- pytest：636（Phase 7 终态）→ **641 passed / 37 skipped**，+5 = 本阶段新增（MCP 网关：工具面 / 双传输 parity / 错误 token / 无 token 拒启 / 枚举面），0 失败。
- vitest：142/28 files → **151 passed / 30 files**，+9 = 本阶段新增（MCP 传输 4 + Skills 5），0 失败。
- 3 条 warning 为既有噪音（OTLP 端点未起时的导出提示 + `websockets` 弃用告警），非本阶段引入。

---

## 8. 交接给终局验收的事项

1. **部署前置（承 Phase 7）**：`migrations/001` 必须已应用（chat 建会话写 `harness_version`）。
2. **若启用 MCP 传输**：额外进程 `python internal_api/mcp_gateway.py`（文件方式，非 `-m`）+ 端口 + `SMARTCS_MCP_TOKEN`（≥16 字节，缺省拒绝启动）；harness 侧配 `SMARTCS_KNOWLEDGE_TRANSPORT=mcp`、`SMARTCS_MCP_URL`、`SMARTCS_MCP_TOKEN`。
3. **若启用 Skills**：`SMARTCS_SKILLS=on`（默认 off）；开启态工具面 +1（`skill_load`），提示词多出「可用技能」区段。
4. **上游反馈素材**（待 dg-piagent skill 升级时回流）：① 仓库自有同名包遮蔽官方 SDK 的绕法；② `bindExtensions()` 必须宿主自 await，否则内建 MCP 扩展永不连接；③ `createAgentSession({tools})` 白名单即完整工具面（含 MCP 工具）；④ 1.0.1 无模型侧技能加载入口（本阶段以 `skill_load` 自建补齐）；⑤ `noSkills: false` 会拉入用户级技能目录（30 个实测）。
5. **未落地项（设计如此）**：Subagent（只评估）、4 个身份绑定工具的 MCP 化（只评估）。

---

**STATUS: completed**

- **8.1 MCP**：`knowledge_search` 经真实 MCP 通道服务，双传输逐条 parity；身份零进通道；默认 http 零回归。三个 SDK 集成发现（同名包遮蔽 / `bindExtensions` 宿主职责 / 白名单即工具面）已记录并将在 skill 升级时回流上游。
- **8.2 Skills**：三份服务器自有技能经显式路径注册（发现恒关闭，实测关闭发现挡住 30 个用户级技能）；描述级披露 + `skill_load`（enum 限定）正文按需；真实 compaction 后仍披露；关闭态零回归。
- **8.3 / P8-4**：两份评估报告，均**只报告不落地**；结论明确（Subagent 维持单 Agent、身份绑定工具留 HTTP 薄壳）。
- **基线（独占窗口，代码冻结后重跑）**：pytest **641 passed / 37 skipped**、vitest **151 passed / 30 files**、`tsc` 干净；白名单与业务目录约束满足；未 commit / 未 push（HEAD `abf71d2`）。

PHASE8_DONE completed
