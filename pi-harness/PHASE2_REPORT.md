# Phase 2 报告：只读业务工具接入

> **执行方**：ds-for-act（本终端） ｜ **日期**：2026-10-03
> **依据**：`../python-impl/docs/HANDOFF-phase2.md` + `../python-impl/docs/phase2-design.md`（已定稿）
> **范围**：只接 5 个 READ 工具；未接任何 WRITE；未产生 pending_action；未拆 RAG；未动公网端点语义；未 commit / 未 push。
> **STATUS: completed**

---

## 0. 结论速览

| 项 | 结论 |
|---|---|
| 交付物 | `internal_api/tools.py`、TS 5 个真实 READ 工具薄壳、intent_label 观测元数据、Python 用例迁入 + 新增 |
| 设计 §5 验收用例 | **P2-1 ～ P2-9 全部通过**（P2-8 见 §3.8 的完整说明） |
| TS 测试 | **93 passed / 0 failed**（18 文件），`tsc --noEmit` 干净 |
| pytest 基线 | **540 passed / 37 skipped**（Phase 1 基线 512 → **+28**，正是迁入 15 + 新增 13） |
| mcp / ToolExecutor 改动 | **零**（`git status` 实测：仅 `.env.example`、`api/main.py`、`internal_api/`、`migrations/`、`tests/`） |
| 偏差 | 11 项，记于 §6；**未静默修改任何计划/设计文档** |
| 阻塞 | 无 |

---

## 1. 实现对照 design §1–§5

| 设计节 | 要求 | 落地 | 证据 |
|---|---|---|---|
| §1 工具清单与 schema 来源 | 5 个 READ 工具；TS schema 从 `mcp/mcp_server.py` **逐字段翻译**，不得发明字段 | `src/agent/tools/business-tools.ts`：5 个 `defineTool`，properties / required / description 逐条对照源定义（含 `top_k` 的 `default: 3`） | `tests/phase2-tools.test.ts` P2-1/P2-9；§6-D3 记录了一处必须修正的翻译错误 |
| §2 内部通道契约 | `POST /internal/tools/execute`；service JWT 的 `business_user_id` 必备；身份字段剥离+审计；force-bind；统一走 ToolExecutor；最小必要文本 + details；大小上限 | `internal_api/tools.py` 全部实现；`service_jwt.decode_service_token(require_business_user_id=True)` | `tests/test_internal_api_tools.py`（13 用例） |
| §2 TS 薄壳 | transport only、零重试、零业务逻辑；AbortSignal 全链透传；details 原样透传 | `business-tools.ts` 的 `execute` 只做一次 HTTP 调用；`signal` 传入 `fetch`（并与超时 `AbortSignal.any` 组合） | P2-4（连接中断）；`tests/phase2-tools.test.ts` |
| §3 意图分类降级 | 正则快速版；`agent_settled` 后异步；只进 receipt metadata；**不得影响执行** | `classifyIntent()` + 写入 `response.metadata.intent_label`；无任何执行路径读取它 | P2-9：refund 文案仍执行模型选择的 `order_query` |
| §4 白名单变更 | 新增 `internal_api/tools.py`、`python-impl/tests/`；`api/main.py` 最小 diff | 见 §2 | `git diff --stat` |
| §5 验收用例 | P2-1 ～ P2-9 | 见 §3 | — |

### 1.1 新增/变更文件

```text
python-impl/
├─ internal_api/tools.py                     ← 新：/internal/tools/execute
├─ internal_api/service_jwt.py               (改：require_business_user_id)
├─ internal_api/__init__.py                  (改：合并 auth + tools 子路由)
├─ tests/internal_api_helpers.py             ← 新：共享 fixture（真实 MySQL + 迁移）
├─ tests/test_internal_api_auth.py           ← 迁入（原 pi-harness/tests/python/）
├─ tests/test_internal_api_history_dispatch.py ← 迁入并改名
├─ tests/test_internal_api_tools.py          ← 新：工具通道 13 用例
├─ api/main.py                               (+46/−2：挂载 + history 分发 + app.state.tool_executor)
└─ .env.example                              (+新变量注释)

pi-harness/src/
├─ agent/tools/business-tools.ts             ← 新：5 个真实 READ 薄壳
├─ business/turn-context.ts                  ← 新：per-turn 身份持有者
├─ business/python-client.ts                 (改：executeTool)
├─ server/chat-pipeline.ts                   (改：发布身份 + intent_label)
└─ session/registry.ts                       (改：SessionRuntime 携带 turnContext)
```

---

## 2. python-impl 改动范围（白名单合规自查）

```
 M .env.example          | +新变量注释（含 Phase 2 的 SMARTCS_TOOL_RESULT_MAX_CHARS）
 M api/main.py           | +46 −2
?? internal_api/         (Phase 1 新增；Phase 2 加 tools.py)
?? migrations/           (Phase 1)
?? tests/internal_api_helpers.py
?? tests/test_internal_api_auth.py
?? tests/test_internal_api_history_dispatch.py
?? tests/test_internal_api_tools.py
```

**`mcp/`、`agents/`、`context/`、`memory/`、`rag/`、`auth/`、`docker-compose.yml` 零改动**（约束 3 要求 ToolExecutor / `mcp_server.py` 一行不改 —— 实测 `git status` 无任何相关条目）。

---

## 3. 设计 §5 验收用例结果表（P2-1 ～ P2-9）

所有 P2 用例都是**真 Python 内部通道 + 真 MCP/ToolExecutor + 真 SQLite 业务数据**（`ORD-20260801-0001` 归属 `user_001`），仅模型为 Faux。

| # | 场景 | 结果 | 证据与关键输出 |
|---|---|---|---|
| P2-1 | 模型经工具链查自己订单 | ✅ PASS | `tests/phase2-tools.test.ts`：模型选 `order_query` → transcript 中的 toolResult 含 `ORD-20260801-0001`、`待付款`、`SQLite 本地国内电商演示数据`，`isError=false`。**真实业务数据，非 fixture** |
| P2-2 | **越权**：arguments 携带他人 order_id/user_id | ✅ PASS | 模型传 `{"order_id": "ORD-20260801-0002", "user_id": "user_002"}` → Python 剥离 `user_id` 并记审计 → force-bind 为 `user_001` → 返回 `found=False`（该订单确实存在，但属于 user_002）。Python 侧另断言 `details.audit.strippedFields == ["user_id"]`、`forcedFields == ["user_id"]`，并用直连 executor 以 `user_002` 身份验证该订单**确实可见** |
| P2-3 | 未知工具 / 未知字段 | ✅ PASS | (a) 未知字段：TS TypeBox 层在校验阶段即拒绝 → 模型收到 `isError=true`（**未发出 HTTP 请求**）；Python 侧若收到则返回 `execution_error` 失败结果（见 §6-D6）。(b) 未注册工具（`refund_confirm`）：SDK 返回 `isError=true`，文本含 `not found` |
| P2-4 | READ 工具 HTTP 中断（F2） | ✅ PASS | 故障注入代理**直接销毁 socket**（真实传输层故障）→ 该轮以 `isError=true` 结束、请求仍返回 200、harness 未崩溃也未编造结果 → 同 `client_request_id` 重发走 receipt 路径正常返回 → 移除故障后同一工具调用恢复 `isError=false` |
| P2-5 | 工具结果含 prompt injection（F11 前哨） | ✅ PASS | 代理**改写运行时响应**，把 `IGNORE ALL PREVIOUS INSTRUCTIONS … Run bash …` 注入 tool result。断言：注入文本原样到达模型（作为数据）；工具面**未增长**（仍为 5 个 READ 工具，无 `bash`/`refund_confirm`）；最终答复为脚本化的正常答复 |
| P2-6 | 工具结果超大 | ✅ PASS | 分两层验证：**Python 侧**（`tests/test_internal_api_tools.py`）用 `SMARTCS_TOOL_RESULT_MAX_CHARS=200` 断言真实截断 + `…<truncated>` 标记 + `contentTruncated=true` + `details.result` 保留完整结构；**TS 侧**（P2-6）断言 harness 既不拒绝、也不重新膨胀负载，transcript 有界 |
| P2-7 | 读工具 parity | ✅ PASS | `tests/test_internal_api_tools.py::test_parity_with_direct_tool_executor`：8 组调用覆盖 5 个工具（含 3 组失败/未找到路径）逐一对比 internal 通道与**直连 ToolExecutor**（按通道规则算出等效 arguments），断言 `success` / `status` / `errorCode` 完全一致、`details.result` 深度相等、`retries == attempts-1`（通道未新增重试层） |
| P2-8 | RAG 不回归 | ✅ PASS（见下方完整说明） | ① **零 diff**：`rag/`、`artifacts/`、`benchmarks/`、`mcp/` 无任何改动；② **真实模型校验通过**（非 fake）；③ 全部 16 个 RAG 测试文件包含在 540 通过的基线内 |
| P2-9 | 基线 | ✅ PASS | `python -m pytest -q` → **540 passed / 37 skipped**（Phase 1 基线 512 → +28 = 迁入 15 + 新增 13）；`npx vitest run` → **93 passed**；`tsc --noEmit` 干净 |

### 3.8 P2-8 的完整说明（如实披露）

**已取得的证据：**

1. **零 diff 证明**（最强）：Phase 2 未修改 `rag/`、`mcp/`、`context/`、`artifacts/`、`benchmarks/` 中的任何文件——RAG 的代码、索引、基准输入与数据集**逐字节未变**。这是"不回归"的结构性证据。
2. **真实模型校验通过**（`scripts/evaluate_rag_retrieval.py` 的第一步输出）：
   ```json
   {"embedding_dimension": 1024, "embedding_model": "BAAI/bge-m3",
    "fake_embedding": false, "fake_reranker": false,
    "reranker_backend": "sentence_transformers",
    "reranker_model": "BAAI/bge-reranker-v2-m3", "status": "ready"}
   ```
   即 RAG 栈在本机可加载**真实** bge-m3 + bge-reranker-v2-m3（非 fake），`status: ready`。
3. **全部 RAG 测试在基线内通过**：16 个 RAG 相关测试文件（`test_rag_*`、`test_knowledge_rag.py`、`test_api_rag_smoke.py`、`test_api_startup_rag.py` 等）均包含在 540 passed 中。

**未能取得的证据（如实报告）：**

**完整 benchmark 的指标复跑（Recall@10 / MRR@10 / nDCG@10 / wrong-domain）未能在本次环境内完成。** 该脚本需要 CPU 上跑 4 个 variant × 全量 query × 3 个 domain 的检索 + 交叉编码器重排；一次 30 分钟预算的运行在 `evaluate_variants()` 阶段被超时终止（未写出 `metrics.json`），随后以 90 分钟预算重跑，结果见下：

> **RAG benchmark 复跑结果：未完成**（三次尝试均未产出 `metrics.json`），完整说明见 §7。

**结论口径**：P2-8 判定为 **PASS**，依据是"零 diff + 真实模型 ready + 全部 RAG 测试通过"这三项；**benchmark 数值复跑属于未完成的加强项**，已明确标注，交验收方裁决是否需要专门的机器预算重跑。

---

## 4. 测试运行命令与最终结果

### 4.1 命令

```bash
# ---- Harness（TS）----
cd D:/Workspace_for_Codex/project005_SmartCS/pi-harness
npm ci
npx tsc --noEmit
npx vitest run
npx vitest run tests/phase2-tools.test.ts        # P2-1 ~ P2-9
npm run dev                                       # 起边缘服务（PORT 默认 8971）

# ---- Python ----
cd D:/Workspace_for_Codex/project005_SmartCS/python-impl
python -m pytest -q                               # 全量基线
python -m pytest tests/test_internal_api_tools.py -q
python -m pytest tests/test_internal_api_auth.py tests/test_internal_api_history_dispatch.py -q

# ---- RAG（P2-8）----
python -m scripts.evaluate_rag_retrieval \
  --benchmark-root benchmarks/rag \
  --artifact-root artifacts/rag_round3/production_indexes \
  --output-root <临时目录>            # 注意：不要指向 artifacts/rag_round3，避免写仓库
```

**前置条件**：compose MySQL `:3307` 在运行；测试库 `smartcs_phase1_test` 存在（`CREATE DATABASE smartcs_phase1_test` 并授权 `smartcs`）。用例自身 DROP/CREATE 该库的表并执行真实迁移，不触碰 `smartcs_checkpoint`。

### 4.2 最终结果

```
$ npx tsc --noEmit
(无输出 = 干净)

$ npx vitest run
 Test Files  18 passed (18)
      Tests  93 passed (93)

$ python -m pytest -q
540 passed, 37 skipped, 1 warning in 140.43s

# 基线对照
Phase 1 基线 : 512 passed / 37 skipped
Phase 2 结束 : 540 passed / 37 skipped   (+28 = 迁入 15 + 新增 13)
```

> **复跑提示（实测）**：RAG benchmark 的交叉编码器进程会占用约 3–4 GB 常驻内存。**在它运行期间跑 `pytest` 或 `vitest` 会导致被测套件出现假失败**（本次实测到一次 pytest `faulthandler` 崩溃与一次 10 项 TS 失败）；进程结束后在干净环境复跑即恢复全绿。请在复跑验收时确保无残留的 benchmark/模型进程（`tasklist | findstr python`，端口见 §4.1）。

新增/迁入的 Python 用例：

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `tests/test_internal_api_auth.py` | 11 | §6 契约全错误码（迁入） |
| `tests/test_internal_api_history_dispatch.py` | 4 | §7 分发决策（迁入） |
| `tests/test_internal_api_tools.py` | 13 | §2 工具通道（新增） |

TS 新增：`tests/phase2-tools.test.ts`（7，P2 门禁）+ `tests/helpers/tool-proxy.ts`（故障注入代理）。

---

## 5. 关键实现说明

- **身份强制绑定是三道而非一道**：① TS schema 允许模型填 `user_id`（忠实翻译源定义），但 ② Python 通道在调用 executor 前**剥离**一切身份字段并审计，③ 再按工具 schema **force-bind** `user_id` = JWT 中的 `business_user_id`。第三道是必需的：`mcp/mcp_server.py` 的 `customer_tool_arguments` 只覆盖 `{order_query, refund_evaluate, refund_create, ticket_create, ticket_query}`，**`risk_check` 不在其中**——若不补，模型可对 `risk_check` 断言任意身份（见 §6-D4，已有专门用例）。
- **工具面不增长**：`READ_TOOLS` 白名单在 Python 侧强制，写入类工具（`refund_create`/`ticket_create`/`refund_confirm`）在该通道上**不可达**而非仅被过滤；TS 侧白名单只注册 5 个 READ 薄壳。P2-5 断言注入攻击后工具面仍为 5 个。
- **零重试落在薄壳上**：TS `execute` 只发一次 HTTP；重试/超时/账簿/授权全部留在 Python ToolExecutor。P2-7 用 `retries == attempts-1` 证明通道未新增重试层。
- **AbortSignal 全链**：`signal` 与超时信号用 `AbortSignal.any` 合并后传给 `fetch`；SSE 断开 → `session.abort()` → 工具 HTTP 中止（P2-4 的 socket 销毁即走这条链）。
- **intent_label 是纯观测**：运行结束后计算并写入 receipt `response.metadata`，同时回传 `meta.intentLabel`；代码中没有任何分支读取它。

---

## 6. 偏差节（与计划/设计不一致之处，均未静默改文档）

**D1 — `user_id` 仍出现在模型可见的 schema 中**
设计 §1 要求"逐字段翻译，不得发明字段"，而 `mcp_server.py` 的 4 个工具确实声明了 `user_id`；设计 §2 又要求"身份字段剥离"。两者同时遵守的结果是：**字段对模型可见，但其值完全无效**（被剥离后重新绑定）。这是设计的明确取舍，但值得指出：模型可能因此产生"我在指定用户"的错觉。**建议**后续阶段评估是否把 `user_id` 从模型可见 schema 中移除（那将是对 §1 的有意偏离，需裁决）。

**D2 — 为 5 个 schema 增加 `additionalProperties: false`（超出字面翻译）**
TypeBox 的 `Type.Object` **默认允许**未声明字段，导致未知字段会穿透到 Python（P2-3 首轮实测失败）。为满足 P2-3「未知字段 → schema 校验拒绝」，5 个 schema 均显式关闭额外属性。该选项只是把 Python `input_schema` 已隐含的"字段集合固定"显式化，未新增/删除任何字段。

**D3 — `order_query.user_id` 必须是 Optional（我最初的翻译错误）**
`mcp_server.py` 中 `order_query` 的 `required` 是 `["order_id"]`，`user_id` 虽声明但可选。我最初译成了必填，导致不传 `user_id` 的调用被 schema 拒绝（P2-5 首轮因此失败）。已按 `required` 逐字段校正。**教训**：翻译 schema 必须同时看 `properties` 与 `required`。

**D4 — `risk_check` 的身份绑定由本通道补齐（源实现未覆盖）**
`customer_tool_arguments()` 的强制绑定集合**不含 `risk_check`**，而 `risk_check` 的 schema 声明了必填 `user_id`。若通道只依赖它，模型就能对风控接口传入任意 `user_id`。通道改为**按工具 schema 驱动**的绑定：凡声明 `user_id` 的工具一律绑定为已验证身份（现有覆盖 + `risk_check`）。**未修改 `mcp/mcp_server.py`**（约束 3），因此这是通道侧的补齐而非源修复；若后续希望统一，应在受控变更中改源实现。

**D5 — 工具级失败走 200 而非 4xx（对 §2 契约的精确化）**
设计契约写 `200 → ok:true`、`4xx/5xx → ok:false`。实现细化为：
- **协议/鉴权/白名单/会话归属**问题 → 4xx/5xx，`ok:false`；
- **工具执行完成但业务结果为否**（如"订单不存在"、参数被工具契约拒绝）→ **200 + ok:true**，真实结果放在 `details.success/status/errorCode/error`。
理由：这类结果必须作为**工具结果**交给模型（它要据此回答用户），而不是让 harness 抛错。TS 薄壳把 4xx/5xx 转成 `isError` 工具结果，把 200 原样透传。

**D6 — 未知字段在 Python 侧是 `execution_error`，不是 4xx**
`ToolExecutor` 把 handler 抛出的 `TypeError` 捕获为 `success=false, error_code='execution_error'`（既有行为，**一行未改**）。因此 P2-3 的"schema 校验拒绝"由 **TS TypeBox 层**承担（在发 HTTP 之前拒绝），Python 侧则是兜底失败结果。已用两个用例分别固定这两层的行为。

**D7 — 保留 fake 工具作为显式测试替身（设计 §2 允许二选一）**
设计 §2 允许"fake 工具层替换为 internal fake HTTP server 或 mock python-client，二选一由执行方定"。本实现的选择是：**生产默认挂载 5 个真实薄壳**（`toolMode: "business"`），Phase 0/1 的 SDK 级测试显式声明 `toolMode: "fake"` 继续使用硬编码替身。理由：Phase 0 用例测的是 SDK 事件/白名单语义，与业务数据无关，改用真实通道只会让它们变慢且更脆。

**D8 — intent 分类为纯正则（设计 §3 允许）**
设计 §3 写"可选：先用正则快速版，LLM 版 Phase 3 评估"。本实现取正则版，5 类 + general，确定性、零延迟、零副作用。

**D9 — `SessionRuntime` 引入 `turnContext`（对 §4 的接口扩展）**
设计 §4 的 registry 只描述 `session: AgentSession`。工具薄壳需要**每轮**的身份，而工具定义在会话创建时注册一次。实现让 registry 的工厂返回值额外携带 `turnContext`，由管线在 `prompt()` 前后 set/clear。安全性依赖 §6.2 的 per-session 单写者不变量（同一个会话不会有两个在途请求），已在 `turn-context.ts` 注释中写明。

**D10 — compose 仍未改动（承接 Phase 1 D3）**
写白名单依然不含 `docker-compose.yml`，设计 §4 也把 compose 放开推迟到 Phase 3+。因此 pi-harness 尚无容器化定义。

**D11 — `.env.example` 增加 `SMARTCS_TOOL_RESULT_MAX_CHARS` 注释**
该变量在实现中用于工具结果截断上限（回落到 `SMARTCS_CONTEXT_TOOL_PREVIEW_CHARS`，再回落 1200）。未新增必填变量。

---

## 7. RAG benchmark 复跑（P2-8 加强项）—— 未完成，如实披露

**结论：完整 benchmark 的指标复跑在本环境内未能完成。** 三次尝试如下：

| 尝试 | 预算 | 结果 |
|---|---|---|
| 1 | 30 min | 在 `evaluate_variants()` 阶段被超时终止；只写出 `model_validation.json`，无 `metrics.json` |
| 2 | 90 min | 同上，运行约 45 min 后仍无指标产出，人工终止 |
| 3 | 25 min | 干净环境单独运行，120 s 后停在同一点（模型已加载、`model_validation.json` 已写出），持续占用 ~3.9 GB 内存但无输出推进 |

**命令**（可复现；注意 `--output-root` 必须指向临时目录，不要写 `artifacts/rag_round3`）：

```bash
cd python-impl
python -m scripts.evaluate_rag_retrieval \
  --benchmark-root benchmarks/rag \
  --artifact-root artifacts/rag_round3/production_indexes \
  --output-root /tmp/rag_out
```

**规模与预期不符**：基准仅 **60 条 query / 95 条 qrel**，索引合计约 **45 MB**（agent_engineering 35M + apple_support 2.4M + global_sparse 8.1M）。这个规模在 CPU 上跑 4 个 variant 不应超过数分钟，因此**"慢"不太可能是纯粹的计算量问题**，更可能是环境相关（本机线程/内存竞争，或 `evaluate_variants()` 内部的某个 variant 路径异常）。

**已排除**：`model_validation.json` 显示 `status: ready`、`fake_embedding: false`、`fake_reranker: false`，即模型加载本身是正常的、用的确实是真实 BAAI 模型；卡点在 `evaluate_variants()` 之内。

**建议**：请验收方决定是否指派一次专门的机器预算（干净环境、独占机器）重跑，或先由计划方诊断 `evaluate_variants()` 在该环境下的行为。**在此之前，P2-8 的判定依据仅是 §3.8 的三项结构性证据，本报告不声称已获得可对比的 benchmark 指标。**

---

## 8. 硬约束合规自查

| 约束 | 状态 |
|---|---|
| 只接 READ；`refund_evaluate` 不产生 pending_action | ✅ 通道白名单仅 5 个 READ 工具；未新增任何 pending 语义 |
| 写白名单（新增 `internal_api/tools.py`、`tests/`、`api/main.py`、`.env.example`） | ✅ `git status` 实测，其余目录零改动 |
| `mcp/mcp_server.py` / ToolExecutor **一行不改** | ✅ `git status` 无相关条目 |
| 身份安全：剥离 + 审计；user_id 永不取自 arguments | ✅ 三道绑定 + 专门用例（含 `risk_check` 覆盖） |
| TS 薄壳零重试零业务逻辑；AbortSignal 全透传 | ✅ 单次 HTTP；`AbortSignal.any` 合并信号 |
| pytest 基线 ≥ 512；pi-harness 全绿；不 commit/push | ✅ 540/37；93 通过；HEAD 仍 `abf71d2` |
| 测试可重复、命令入报告、全绿或如实列失败 | ✅ §4.1；93+540 全绿；P2-8 的未完成项已如实标注 |
| 偏差记报告，不静默改文档 | ✅ §6，11 项；计划/设计文档零修改 |

---

**STATUS: completed**

- 设计 §5 验收：**P2-1 ～ P2-9 全部通过**（P2-8 的 benchmark 数值复跑作为加强项另行标注）
- TS：**93 passed / 0 failed**（18 文件），typecheck 干净
- Python：**540 passed / 37 skipped**（Phase 1 基线 512 → +28）
- `mcp/`、`ToolExecutor`、`rag/`：**零改动**
- 未 commit / 未 push / 未越白名单
