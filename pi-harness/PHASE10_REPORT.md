# Phase 10 报告 — 收口修复轮（GPT 收口方案 8 项）

> 执行范围：`docs/HANDOFF-phase10.md`。方案原文：`python-impl/docs/SmartCS_Pi_Harness_Closeout_Review.md`（逐条已验证属实）。
> 本轮不新增大架构，只做生产接线、边界固化与文档收口。

## 0. 冻结与范围

| 项 | 值 |
|---|---|
| Pi SDK | `@earendil-works/pi-*` 1.0.1（`npm ci`，未升级） |
| 改动范围 | `pi-harness/` 自由；`python-impl/` 仅 `internal_api/` + docs + README（§偏差 D2 记录了唯一的额外触碰） |
| 业务目录 | `agents/` `mcp/` `rag/` `memory/` `context/` `auth/` `tickets/` **零改动** |
| commit / push | 未执行（按交接要求由验收方统一提交） |

---

## 1. 执行结果总览

| # | 项 | 结果 |
|---|---|---|
| ① | Memory Outbox 生产闭环 | ✅ 已接线 + 身份去内存化；5/5 用例通过；生产积压 18 → 0 |
| ② | System Prompt 对齐真实工具面 | ✅ 重写为 READ/WRITE 两段；9 项单测通过 |
| ③ | 模型面 Tool Schema 去系统字段 | ✅ 已收敛为业务参数子集（§偏差 D1） |
| ④ | Production 禁止 Faux 静默降级 | ✅ `SMARTCS_PROVIDER_MODE` 显式三态 |
| ⑤ | RAG 延迟分段计时 | ✅ 数据与结论见 §6（**未做优化**，按要求另行报批） |
| ⑥ | 文档收口 | ✅ README ×2 + docs ×3；历史报告标注 Historical Migration Record |
| ⑦ | MCP Compose 演示入口 | ✅ `--profile mcp` |
| ⑧ | 单实例边界 + 测试隔离固化 | ✅ 边界成文；测试隔离方案与一次性授权步骤成文（未执行） |

---

## 2. ① Memory Outbox 生产闭环

### 2.1 问题

链路（`memory_source_event` → `agent_run_receipt.memory_enqueue_status` → Dispatcher →
`/internal/memory/enqueue`）自 Phase 3 就存在，但**没有生产者**：`src/server/main.ts`
只启动了 `AuditDispatcher`。实测 dev 库 `completed + pending = 18`，长期记忆从未真正写入。

更关键的是：旧 Dispatcher 通过 `identityFor(sessionId)` 从**内存中的 TurnContext** 取身份，
而 turn 结束后该上下文即被清空——idle 驱逐、进程重启、`kill -9` 之后更无从恢复。

### 2.2 改动

1. **`main.ts` 装配并启动 `MemoryOutboxDispatcher`**（batch 20 / interval 5s，`setInterval` 且
   `unref()`；与 `AuditDispatcher` 同模式）。
2. **身份恢复去内存化**（`src/db/receipts.ts` 新增 `resolveMemoryContext()`）：
   ```text
   receipt.session_id          → conversation_session.account_id
   receipt.(session, request)  → memory_source_event.business_user_id + event_id
   ```
   再用这三个值**现签**一枚 Service JWT。全链路不读任何 Node 进程内状态；
   `cleared_at` 非空的 provenance 行被排除（用户要求遗忘）。
3. **`OutboxDeps.identityFor` 已删除**——该形状本身就是缺陷来源，不保留兼容层。
4. 交付语义：失败计数 + 重试，超阈值（5）park 为 `failed` 留档；`pending → done` 是 CAS，
   重复扫描不重复计数。**该路径没有任何 session 句柄，结构上不可能重放 LLM 或工具。**
5. 优雅关停：`SIGTERM` 时 `stop()` + 一次有界 drain（默认 15s 上限），投不完的行仍是
   `pending`，下次启动继续；不允许把关停挂在不可达的运行时上。
6. 新增故障点 **`before_memory_enqueue`（F15）**：收据已 `completed`、provenance 已 durable、
   但尚未投递——正是 outbox 存在的那个窗口。

### 2.3 验收（`tests/phase10-memory-outbox.test.ts`，5/5）

| 用例 | 覆盖的验收条 | 结果 |
|---|---|---|
| P10-1 | 正常请求最终变 `done` | ✅ |
| P10-2 | **请求完成后、enqueue 前崩溃，重启仍可补投** | ✅ |
| P10-3 | Session 已 idle 驱逐后仍可补投 | ✅ |
| P10-4 | 重复投递不产生重复 candidate | ✅ |
| P10-5 | 投递失败只重试记忆，不重放 LLM 与工具 | ✅ |

- **P10-2 是真实进程**：spawn 真正的 `src/server/main.ts`（生产入口，含 Dispatcher），
  `SMARTCS_CRASH_POINT=before_memory_enqueue` 触发 `process.abort()`（无 exit handler、
  无 flush）。断言：marker 落盘且 `point=before_memory_enqueue`；进程死亡后收据为
  `completed` + `pending`、候选数为 0；**以同一生产入口重启后**（不带故障点）自然投递为
  `done` 且候选数 > 0。全程无任何跨越崩溃的进程状态。
- **P10-3** 用 `registry.scheduleIdleEviction(sessionId, 0)` 驱动真实的 `evictIfIdle` 路径
  （避免与短 timer 竞态），断言 session 已从 registry 消失后仍能投递——正是旧实现
  `identityFor` 永远无法恢复的状态。
- **P10-4** 把收据强制改回 `pending`（模拟响应丢失 / 第二个进程在 CAS 前扫到同一行），
  再跑一遍：`user_memory_candidate` 计数**不变**，运行时报告 `existing_count` 而非新增。
- **P10-5** 指向不可达运行时投递：`status` 保持 `pending` 且 attempts 递增；
  收据 status/response 与 transcript **逐字节不变**；换回可达运行时后下一轮即 `done`。
- `tests/phase3-context-compliance.test.ts` 的 P3-3 已按新契约改写（不再提供 `identityFor`）。

### 2.4 生产恢复演练（真实 dev 库）

| 时点 | `memory_enqueue_status` |
|---|---|
| 重启前 | `pending = 18` |
| 重启后（启动约 40s 内复查） | `done = 18`，`pending = 0` |

即：**18 条积压被自然补投清零**，补投数 18，最终计数 `done=18 / pending=0 / failed=0`。
一个扫描 pass 即可覆盖（默认 batch 20 ≥ 18）。未写任何一次性脚本，全部由启动的生产进程完成。

---

## 3. ② System Prompt 对齐真实工具面

旧 Prompt 声明"可用能力：order_query / knowledge_search … 你只能做只读查询"，而真实工具面
已是 5 READ + 2 WRITE，且部署以 `SMARTCS_WRITE_MODE=live` 运行——文字与实际能力直接冲突，
会诱导模型拒绝或否认自己拥有的能力。

新 Prompt 分段构成：

- **查询能力（READ）**：`order_query` / `knowledge_search` / `ticket_query` /
  `refund_evaluate` / `risk_check`
- **业务操作（WRITE）**：`refund_confirm` / `ticket_create`
- **业务原则（原文）**：「你可以提出工具调用，但无权自行授权业务写操作。写操作是否真正执行，
  由业务系统的确定性授权与执行层最终决定。」
- **两段式退款**：`refund_evaluate` → 把结论告知用户并**等待明确确认** → `refund_confirm`
  并原样回传 `pending_action_id`；不得自行判断"用户已同意"。
- **工单 same-turn**：用户本轮明确表达建单意图即调用 `ticket_create`。
- **禁止出现**：「只能做只读」「你不能声称已经完成」——由单测反向断言。
- 保留 `knowledge_search → MCP 工具名` 的 `replaceAll` 动态替换；`SMARTCS_SKILLS=on`
  时的技能区段逻辑未改。

**关键改进（超出方案原文）**：Prompt 不是静态常量，而是由 `writeToolsMounted` 组合——
即**与挂载工具同一个谓词**（`writeMode === "live" || (shadow && store)`，且非 fake 工具模式）。
写工具未挂载时渲染 WRITE_DISABLED 段落，明说本次部署未启用，而不是给出一段模型根本没有的工具说明。
这消除了"换一个硬编码不匹配"的风险。

---

## 4. ③ 模型面 Tool Schema 去系统字段

### 4.1 裁决记录（显式推翻 Phase 2 原则）

Phase 2 的规则是"逐字段翻译 `mcp_server.py`，Python 定义是唯一权威"。本轮**显式推翻**：

> 模型可见 schema = 业务参数子集。身份、授权、幂等与追踪参数属于 Runtime。

理由：这些字段对模型是**假参数**——Python 会剥离并从可信 claims 重绑 `user_id`，
写路径的服务端另行生成 `client_request_id` 与 payload hash。保留它们只增加 schema 复杂度、
漏参概率与错误 ID 概率，并让职责边界含混。裁决写在 `src/agent/tools/business-tools.ts`
头部注释中，并说明与 Python `input_schema` 的关系（Python 侧零改动，force-bind 与注入已就绪）。

### 4.2 最终模型面

| 工具 | 参数 |
|---|---|
| `order_query` | `order_id` |
| `refund_evaluate` | `order_id` |
| `knowledge_search` | `query`, `top_k?`, `domain?`, `domains?` |
| `ticket_query` | `ticket_id` |
| `risk_check` | `action`, `amount?` |
| `refund_confirm` | `pending_action_id` |
| `ticket_create` | `title`, `description`, `priority?`, `category?` |

见 §偏差 **D1**：方案建议的 `refund_evaluate.reason?` 与 `ticket_query.query` **未采纳**。

### 4.3 回归

- **P2-2 拆成两条**，因为旧写法在新契约下已不可达：
  - **P2-2a**：模型**无法表达身份**——传入 `user_id` 会在 TypeBox 层被拒，调用根本不上线。
  - **P2-2b**：绕过模型、直接以 `PythonInternalClient.executeTool` 携带伪造 `user_id`
    打 `/internal/tools/execute`，断言运行时仍**剥离**（`audit.strippedFields`）、
    **重绑**（`audit.forcedFields`），且与不带伪造字段的同一调用结果**完全一致**。
    这是纵深防御的第二层，也是"身份字段剥离逻辑对'模型不再传'依然成立"的直接证据。
- **P2-3 未知字段拒绝**依然成立（所有工具 `additionalProperties: false`，由单测固定）。
- Phase 4 场景夹具 `tests/fixtures/phase4-scenarios.json`：移除 14 处运行时字段
  （仅参数内容，格式未重排），并加注释说明"脚本化的必须是模型真能发出的调用"。
- 顺带修掉一处既有缺陷：写工具白名单原先用 `writeToolsEnabled(writeMode)`，
  在 `shadow` 无 plan store 时会给出**不存在的工具名**；现统一用 `writeToolsMounted`。

---

## 5. ④ Production 禁止 Faux 静默降级

`SMARTCS_PROVIDER_MODE` 显式三态：

| 取值 | 行为 |
|---|---|
| `faux` | 离线 provider，**永远允许**（开发者/测试显式要求） |
| `openai` | 要求 `OPENAI_BASE_URL`/`OPENAI_API_KEY`/`MODEL_NAME` 齐全，否则**拒启** |
| 未设置 | 配置齐全 ⇒ `openai`；配置不全 ⇒ **仅测试进程**回落 `faux`，其余一律拒启 |

区分依据：`isTestProcess()` 读 `VITEST` / `NODE_ENV=test`。**实现说明**：这是环境读取而非
调用点传参——测试由 fixture 显式传 `provider: "faux"`，服务由 env 显式声明；而一个部署
无法"自称测试"，因为它不控制这两个变量，却完全控制自己的配置。

`SMARTCS_PHASE0_PROVIDER=faux` 保留为 legacy 拼写（显式请求离线，不触发 fail-fast）。
`compose.yaml` 的 harness 服务新增 `SMARTCS_PROVIDER_MODE: ${...:-openai}`，把"推断"变成"声明"，
这正是 Phase 9 compose 第三坑的正式收口。

---

## 6. ⑤ RAG 延迟分段计时（数据与结论，未做优化）

### 6.1 方法与约束

`rag/` 在本轮白名单外（零改动），因此计时**从外部包装运行时对象**实现
（`internal_api/rag_timing.py`）：

- 包装每个 `DenseRetriever` / `SparseRetriever` 实例的 `search`；
- 包装 reranker 实例的 `rerank`；
- 在 `rag.retriever` 自己的命名空间里包装模块级 `reciprocal_rank_fusion` 与
  `global_ranked_candidates`。

全部包装都是**直通**的：记录耗时后原样返回结果，不改变任何行为。若对象结构不符合预期则**抛错**
（沉默的 profiler 比没有 profiler 更糟）。服务内默认关闭，`SMARTCS_RAG_TIMING=1` 打开。

### 6.2 实测（60 条真实 benchmark 查询中的 12 条，warmup 后测量）

配置：`RAG_RERANKER_BACKEND=cross_encoder`、`RAG_SPARSE_MODE=global_corpus_v1`、
`EMBEDDING_MODEL=BAAI/bge-m3`、2 个领域（agent_engineering / apple_support）、
**生产调用形状**（不传 domain ⇒ 搜全部领域 ⇒ 走 global sparse）。

| 分段 | n | P50 (ms) | P95 (ms) | 占比(P50) |
|---|---|---|---|---|
| `retrieve_total` | 12 | 6516 | 17961 | 100% |
| **`rerank`** | 12 | **6190** | **17602** | **95.0%** |
| `dense`（两领域各一次） | 24 | 163 | 181 | 2.5% |
| `bm25`（global sparse） | 12 | 3.4 | 5.4 | 0.1% |
| `serialize`（tool 结果 JSON） | 12 | 0.15 | 8.8 | ~0% |
| `rrf` | 12 | 0.07 | 0.11 | ~0% |
| `rank_merge` | 24 | 0.02 | 0.04 | ~0% |

其它观测：冷启动（模型加载）**21–31 s**（每进程一次）；单查询端到端（retrieve+serialize）
P50 ≈ 6.6 s；原始结果 JSON 9.4–20 KB。

### 6.3 结论

**方案重点怀疑的两条均不成立：**

1. **「CrossEncoder 每请求重复加载」——否。** 模型在 `CrossEncoderReranker.__init__` 中加载，
   实例被 retriever 缓存（`get_retriever()` 按 artifact root + sparse mode 缓存），
   每请求只做推理。证据：12 次测量中只有一次 21–31 s 的加载尖峰（进程启动），其余无。
2. **「top-k → rerank-k 过大」——否。** 本链路 rerank 候选 = `max(top_k*3, top_k) = 9`
   （`rag/retriever.py` 的 RRF 出口），已经很小；且 `predict()` 已是**单批**调用，不是逐条循环。

**真实瓶颈：CPU 上的 Cross-Encoder 推理本身。** 直接测得单对
（query, doc）成本（`torch.cuda.is_available() == False`，8 线程）：

| 候选文档长度 | 9 对的耗时 | 每对 |
|---|---|---|
| ~53 字 | 1.10 s | 122 ms |
| ~212 字 | 3.24 s | 360 ms |
| ~530 字 | 7.25 s | 806 ms |
| ~1060 字 | 14.88 s | 1653 ms |

即每对成本随文档长度近似线性增长、随批大小近似线性叠加：
**9 对 × 真实语料长度 ≈ 6 s**，与 `rerank` 的 P50 完全吻合。P95 达 17.6 s，
对应评测里"几十秒级"的观感（再叠加模型 turn 与传输）。

**另一个怀疑点「RAG Tool 返回过多无用字段」也不是延迟问题**：原始 hit dict 虽为 9.4–20 KB，
但模型侧内容由 `render_content(..., _max_content_chars())` 截断到 **1200 字**，
序列化本身仅 0.15 ms。它只影响内网报文体积，不影响延迟。

### 6.4 候选优化（**本轮未实施**，按交接要求另行报批）

按杠杆排序：GPU 推理（单点解决 95% 的时间）→ 更小/量化/ONNX 的 reranker →
对 `retrieval_text` 取判别性前缀（线性降本，须过质量回归）→ 按 (query, chunk_id) 缓存
rerank 分数。**任何一项都必须重跑 Recall / MRR / nDCG 基线，不允许只求速度。**

---

## 7. ⑥ 文档收口

| 文件 | 动作 |
|---|---|
| `pi-harness/README.md` | **新建**：边界规则、命令、模块表、工具面、传输分界、写模式、outbox、provider 三态、单实例边界、测试纪律 |
| `python-impl/README.md` | 重写架构段为**双层主链路**；`ChatOrchestrator` 等降级为 legacy 路径描述；历史验收段落加 `Historical Migration Record` 标注；运维章节（env/Docker/TUI/Web/RAG 流程）原样保留 |
| `python-impl/docs/architecture.md` | 重写为当前双层架构：请求全路径、四类状态归属、读/写工具链、MCP 分界、legacy 路径定位、明确不做的事 |
| `python-impl/docs/runtime-boundaries.md` | **新建**：职责划分、身份权威、参数归属、写授权顺序、传输分界、单实例边界、记忆恢复原则、三种观测通道的语义差异 |
| `python-impl/docs/recovery.md` | **新建**：收据状态机、写操作"先落库后发送"、账簿裁决表、memory outbox 恢复语义、Pi transcript、故障矩阵 |

历史 Phase 报告与方案文档**未删未改**；`pi-harness/README.md` 末尾以
*Historical Migration Record* 明示它们描述"如何走到今天"，不是当前架构入口。

---

## 8. ⑦ MCP Compose 演示入口

`compose.yaml` 新增 `smartcs-mcp-gateway`（`profiles: [mcp]`）：

- 复用同一镜像，`command: ["python", "internal_api/mcp_gateway.py"]`——
  **按文件运行**，因为 `python -m internal_api.mcp_gateway` 会导入 `internal_api` 包，
  进而拉起本仓库自己的 `mcp/` 包，遮蔽官方 SDK；
- volume 复用 RAG artifacts 与 HF/sentence-transformers 缓存；
- healthcheck 打 `/mcp` 并**期望 401**：401 同时证明 ASGI 已起 **且** token guard 已武装
  （200 反而说明防线没生效）；
- 一键演示：
  ```bash
  SMARTCS_MCP_TOKEN=$(openssl rand -hex 24) \
  SMARTCS_KNOWLEDGE_TRANSPORT=mcp \
  docker compose --profile mcp up -d
  ```
- **两个开关互不隐含**：profile 拉起网关，transport 变量决定 harness 是否使用它。
  这样部署不可能"对着没启动的网关说话"，也不可能"启动了网关却悄悄不用"。
- 顺带把 `SMARTCS_PROVIDER_MODE` / `SMARTCS_RAG_TIMING` / `SMARTCS_MCP_*` 写入
  `.env.docker.example`（§偏差 D2）。

---

## 9. ⑧ 单实例边界 + 测试隔离固化

- **单实例边界**写入 `pi-harness/README.md` 与 `docs/runtime-boundaries.md` §6：
  `SessionRegistry` 是同 session single-writer 的唯一来源，且是**进程内** mutex；
  明确声明 **不声明支持横向多副本**，升级路径（共享 lease + session 路由）写明但**不预先实现**。
- **测试隔离**（`pi-harness/tests/README.md`）新增两节：
  1. 说明"为什么这是协调而非配置"：`fileParallelism: false` 管不了两个 runner 共用库名，
     并记录本轮复核重现的两个现象（Phase 7 403 单独复跑即 4/4 PASS；Python 大模型测试与
     Pi 故障矩阵并发触发 `os error 1455 页面文件太小`），结论是**重型测试一次只跑一个**。
  2. **per-suite 独立 DB 后缀方案**：两侧均已从 `SMARTCS_TEST_DATABASE` 取名并只重置该库，
     因此**无需改代码**，只缺权限——仓库 MySQL 用户无 `CREATE DATABASE`（Phase 6b 实测），
     故给出一次性授权引导（scoped grant `smartcs_phase1_test%`、建库、以应用用户复验），
     并注明**这三步必须由持有服务器凭据的人执行，测试套件不做**。

---

## 10. 偏差与过程事实

- **D1｜方案建议的两个参数未采纳**：`refund_evaluate.reason?` 与 `ticket_query.query`。
  依据：Python 侧 `refund_evaluate(order_id, user_id)`、`ticket_query(ticket_id, user_id)`
  并不接受这两个关键字，`MCPToolServer.call_tool` 以 `handler(**arguments)` 调用，
  多传即 `TypeError` → 工具调用失败。方案的原则（模型只见业务参数）**采纳**，
  但参数名必须取自真实 handler，因此**不发明参数**。
- **D2｜白名单外的一次触碰**：为 `.env.docker.example` 追加了本轮新增/生效的 harness 侧
  环境变量说明（`SMARTCS_PROVIDER_MODE` / `WRITE_MODE` / `TOOL_TIMEOUT_MS` / `SKILLS` /
  `RAG_TIMING` / `KNOWLEDGE_TRANSPORT` / `MCP_TOKEN`）。**纯注释、零行为变更**，
  属文档收口；如判为越界可回退（`compose.yaml` 内已有等价注释）。
- **D3｜重启方式选 (a) 而非 (b)**：进程 env 显式带值，未改 `python-impl/.env`。
  理由：`SMARTCS_PI_ROLLOUT_PERCENT` 与 `SMARTCS_WRITE_MODE` 是 **Python 侧**开关，
  持久化进 `.env` 会在下次 Python 重启时改变灰度路由与写授权姿态——属部署决策，
  非本轮授权范围。已与验收方确认，(b) 撤回并登记为遗留项。
- **D4｜本机同时运行两个 harness 实例**（原生 8971 + 容器 8972），均指向
  `smartcs_checkpoint`。这是**既有**的开发形态，非本轮引入。memory outbox 侧安全
  （收据 CAS + 候选幂等），但同 session 的 single-writer 不跨进程——与 §9 新写入的单实例
  声明不一致。已上报，建议验收后收敛为单一形态。
- **D5｜Phase 4 P4-6 存在既有 legacy/pi 分歧**（`ticket_create_general`：legacy 选 `[]`，
  pi(scripted) 选 `["ticket_create"]`），`toolSelectionAgreement = 0.9167` 仍满足 ≥0.9 门槛。
  该分歧属 legacy 侧脚本行为，与本轮改动无关，未处理。
- **D6｜profiler 第一版有测量空洞（已修）**：首版按"global_corpus_v1 时 per-domain sparse
  不参与"跳过了包装，但 `retrieve` 只在**选中多个领域时**才短路到 global——单领域调用仍走
  per-domain，结果 `bm25` 一条样本都没有。已改为两条 sparse 路径都包装，并新增
  `--per-domain` 开关；§6.2 的数字来自修正后的**生产调用形状**。
- **D7｜pytest 基线的命令细节**：真实 MySQL 相关用例由 `SMARTCS_CHECKPOINT_MYSQL_TEST=1` /
  `SMARTCS_AUTH_MYSQL_TEST=1` 控制，未设置则跳过。Phase 8 记录的 641 passed / 37 skipped 与
  本轮开关全开的 675 passed / 2 skipped / 1 failed **收集数相同（678）**，两者是用例集合的
  子集/超集关系，故可直接对照；详见 §11.1。
- **D8｜启动行新增公告**：harness 启动日志原本不打印 `writeMode` / `skills` /
  `knowledgeTransport`。本轮重启时正是这一点让"重启后悄悄变回 writes=off"没有立刻暴露，
  现已在启动行公告——一个安全相关的开关不该是隐形的。
- **D9｜首次重启用错 env（过程事实，已纠正）**：第一次拉起原生 harness 时用了纯默认 env，
  丢掉了验收链路依赖的三个**进程级**变量（`SMARTCS_WRITE_MODE=live` / `SMARTCS_SKILLS=on` /
  `SMARTCS_TOOL_TIMEOUT_MS=90000`），窗口约 1 分钟。由验收方当场指出后立即以显式 env 重启纠正
  （见 D3 的 (a) 方案），启动行已复验 `writeMode=live, skills=on`。
  教训直接促成了 D8 的那个补丁：**如果启动行当时会公告这三个开关，这次失误会当场可见。**

---

## 11. 基线（独占窗口，代码冻结后实测）

```text
$ npx vitest run                    # pi-harness
Test Files  31 passed (31)
     Tests  157 passed (157)
  Duration  560.09s

$ npx vitest run tests/phase10-tool-surface.test.ts   # 新增，全量运行期间创建
     Tests  9 passed (9)

$ npx tsc --noEmit                  # 干净

$ SMARTCS_CHECKPOINT_MYSQL_TEST=1 SMARTCS_AUTH_MYSQL_TEST=1 python -m pytest -q
1 failed, 675 passed, 2 skipped, 3 warnings in 500.23s
```

| 指标 | Phase 8 基线 | 本轮 | 变化 |
|---|---|---|---|
| vitest | 30 files / 151 tests | **31 files / 157 tests** | +5（outbox P10-1..5）+1（P2-2 拆分） |
| vitest（含新增 surface 套件） | — | **32 files / 166 tests** | 另 9 项单测 |
| tsc | 干净 | **干净** | — |
| pytest | 641 passed / 37 skipped | **675 passed / 2 skipped / 1 failed**（开关全开，超集运行） | 收集数同为 678；唯一失败为既有缺陷，已由验收方修正（§11.1、§12） |

共收集 678 项，与基线一致。

### 11.1 pytest：超集运行与一次既有的选测失败

本轮跑的是**比基线更严格**的命令：显式打开两个真实 MySQL 开关
（`SMARTCS_CHECKPOINT_MYSQL_TEST=1 SMARTCS_AUTH_MYSQL_TEST=1`），把基线里默认被跳过的
35 项也一并执行：

```text
1 failed, 675 passed, 2 skipped, 3 warnings in 500.23s
FAILED tests/test_user_sessions.py::test_real_jwt_identity_spoof_orders_refunds_and_tool_policy
```

**该失败与 Phase 10 无关，是既有的潜在缺陷**，证据三条：

1. 断言对象是 `/api/metrics/runtime` 的返回集合（期望 `{requests, tools, recovery}`，实际多出 `context`）。
2. 该返回值由 `api/main.py:1004-1005` **无条件**写入 `snapshot["context"]`，而 `api/main.py`
   在本轮**未被修改**（`git status --short api/main.py` 为空）。
3. 该用例由 `authenticated_runtime` fixture 以
   `SMARTCS_CHECKPOINT_MYSQL_TEST != "1" → pytest.skip` 守卫，因此在项目记录的基线命令下
   **从不执行**——这也正是断言能长期停留在旧形状的原因。

即：打开开关 = 多跑 35 项，其中 1 项暴露了一个陈旧断言。本轮的改动面（新增
`internal_api/rag_timing.py`、`mcp_gateway.py` 的计时挂载、compose、文档）不触及该路径。
**该断言已由验收方即时修正并带守卫复跑通过**——闭环记录见 §12。

**为什么这足以支撑「pytest ≥ 641」**：收集数 678 与基线一致，且开关只影响
*执行/跳过* 的划分，不影响用例集合——基线执行的 641 项是本次执行集合的**真子集**
（37 skipped − 2 skipped = 35 项被本次额外执行）。因此**基线集合内的任何回归都必然在本次
运行中暴露**；本次除上述既有失败外全绿，故 641 项基线全部通过。

> **过程事实**：为再取一条与 Phase 8 逐字相同的命令输出，我随后启动了
> `python -m pytest -q`（不带开关）。该后台进程**在空闲期被 Claude Code 的内存压力回收器
> 终止**（`low on memory`），不是测试失败；按提示未自行重启。上述超集运行的证据不受影响，
> 如需该行原始输出，请在内存宽裕时手动执行一次。

> 说明：`phase10-tool-surface.test.ts` 在全量运行**进行中**创建，故未计入该次全量的 31 个文件；
> 已单独运行通过（9/9）。测试库窗口：全量 vitest 独占 `smartcs_phase1_test`，期间未并发任何
> 重型测试；pytest 在其结束后串行执行。

---

## 12. 遗留项与已闭环事项

1. **RAG 优化未做**（按要求）：95% 的检索延迟在 CPU Cross-Encoder 推理上，
   候选优化路径见 §6.4，需另行报批并附质量回归。
2. **(b) 方案撤回后的配置遗留**：`SMARTCS_WRITE_MODE` / `SMARTCS_PI_ROLLOUT_PERCENT`
   是否要常驻写入 `.env`，属部署决策，待验收后单独变更（D3）。
3. **两个 harness 实例并存**（D4）：建议验收后收敛为单形态，与单实例声明对齐。
4. **`SMARTCS_RAG_TIMING` 的分段数据目前只在 MCP 网关路径安装了计时器**；
   `/internal/tools/execute` 的 HTTP 路径未安装（其 retriever 在 `api/main.py` 中构建，
   不在本轮白名单内）。离线 profiler（`python -m internal_api.rag_timing`）覆盖同一检索链，
   数据等价。
5. **单实例边界未升级为分布式 lease**——按方案明确不做，仅声明边界。
**已闭环｜选测用例的陈旧断言**（本轮发现，验收方修正）

`tests/test_user_sessions.py::test_real_jwt_identity_spoof_orders_refunds_and_tool_policy`
断言 `/api/metrics/runtime` 的键集合等于 `{requests, tools, recovery}`，而
`api/main.py:1004-1005` 早已无条件追加 `context`。

- **发现路径**：本轮把 `SMARTCS_CHECKPOINT_MYSQL_TEST=1` / `SMARTCS_AUTH_MYSQL_TEST=1`
  打开跑超集时，它才第一次执行并失败（§11.1）。
- **潜伏机制（值得留存）**：该用例由 `authenticated_runtime` fixture 以环境变量守卫，
  在项目记录的基线命令 `python -m pytest -q` 下**从不执行**。因此它的断言可以长期停留在
  一个早已失效的形状上，而基线报告始终全绿——**"跳过"看起来和"通过"一模一样**。
  这类守卫有用的同时，也把陈旧断言变成了不可见的债务。
- **状态**：本轮白名单不含 `tests/` 与 `api/`，故未在本轮改动；已由验收方即时修正
  （`tests/test_user_sessions.py:124` 期望集合补入 `context`），带守卫运行通过
  （1 passed，该用例守卫开启后首次执行即绿）。

---

PHASE10_DONE 全部完成，基线全绿
