# SmartCS 工程化优化计划

> 分支：`feat/apple-intent-routing`
> 生成日期：2026-07-12
> 目标：将四个核心链路（意图路由、RAG 知识检索、工单创建、合规审查）做深工程化，并完成「金融域 → Apple 售后域」重构收尾。

---

## 1. 当前状态诊断

分支正处于「金融域 → Apple 售后域」重构的中途。设计文档 `docs/superpowers/plans/2026-07-11-apple-support-intent-routing.md` 列了 6 个 Task，实际完成度：

| Task | 内容 | 状态 |
|---|---|---|
| 1 | `agents/intent_models.py`（taxonomy + schema + `build_intent_decision`） | ❌ **模块不存在，构建阻断** |
| 2 | `intent_router.py` 结构化解析/修复/降级 | ✅ 已完成（但 import 了不存在的模块） |
| 3 | Supervisor 澄清 + 安全意图处置 | ⚠️ 测试已写，实现缺失 |
| 4 | 实体 TTL + `INTENT_*` 配置 | ❌ 未开始 |
| 5 | Golden dataset + 离线评测 | ❌ 未开始 |
| 6 | 文档 / 全量回归 | ⚠️ README/.env 部分更新 |

### 头号阻断问题

`agents/intent_router.py:32` 导入 `agents.intent_models`，但该文件不存在。`agents/__init__.py` 级联触发导入失败，6 个测试文件在收集阶段崩溃（已实测确认）：

```
ERROR tests/test_intent_router.py / test_supervisor.py / test_routing.py
      test_knowledge_rag.py / test_ticket_handler.py / test_working_memory_activation.py
ModuleNotFoundError: No module named 'agents.intent_models'
```

### 系统性割裂

意图路由已经 Apple 化，但下游三个 Agent（RAG / 工单 / 合规）仍是金融域残留。`tests/conftest.py` 的 `_normalize_intent_payload` 旧→新映射就是为临时桥接这个割裂而存在。

---

## 2. 四个核心领域的工程化短板

### ① 意图路由（intent_router / supervisor）

- `intent_models.py` 缺失（阻断一切）。契约已被测试和 router 双向锁定：
  - `PrimaryIntent`：优先级 `security > action > query > consultation > complaint > unknown`
  - `SecondaryIntent`：16 项（knowledge_rag 组 6 + ticket_handler 组 6 + compliance_checker 组 4）
  - `AgentTarget`、`ReasonCode`、`IntentCandidate`
  - `IntentEntities`：`extra="forbid"`、值 ≤128 字符、白名单 7 字段（order_id / ticket_id / product / device_model / subscription / account_issue / region）
  - `IntentDecision`
  - `build_intent_decision()`：服务端重算 agent + 候选安全优先合并 + 候选去重降序限 3
- Supervisor 缺 `build_clarification_message()` 与安全意图处置分支。当前 `synthesize_response` 对 `account_security` 等只走通用兜底 `"抱歉，暂时无法处理您的请求，请稍后重试。"`，测试断言的「不要提供验证码 / iforgot.apple.com / 保留证据」全部拿不到。
- `intent_router_node` 硬编码澄清文案含「退款/开户」（金融词），与 Apple 售后测试断言 `"开户" not in final_response` 冲突。
- 实体无 TTL / 版本化，跨轮 `accumulated_entities` 只增不失效，多轮对话累积脏实体。

### ② RAG 知识检索（knowledge_rag）

- `RAG_SYSTEM_PROMPT` 仍是金融域（「金融产品信息…以合同条款为准」「风险提示」）。
- `process()` 的 query 增强分支判断 `secondary in ("product_inquiry", "policy_inquiry", "rate_inquiry")` —— 这些二级意图在新 taxonomy 里根本不存在，整段是死代码。
- rerank 依赖 LLM 返回 `0,2,4` 逗号串，解析失败直接 `documents[:top_k]`，无分数兜底排序；无引用去重、未接入 `min_score`（`long_term.py` 支持但 agent 层没传）。

### ③ 工单创建（ticket_handler）

- `TICKET_SYSTEM_PROMPT` 工单类型是 `refund/claim/account_open`（理赔/开户，金融域），Apple 售后应是维修/退货/订阅取消等。
- 订单判定 `entity_id.startswith("ORD")` 硬编码前缀，脆弱。
- `create_ticket` 里 MCP 调用失败静默吞掉；无 ticket_type 枚举校验、无幂等键。
- `mcp/mcp_server.py` 工具桩返回金融 mock 数据（智能理财产品A）。

### ④ 合规审查（compliance_checker）

- 完全金融域：`FORBIDDEN_TERMS = 保证收益/稳赚不赔/保本保息...`，`COMPLIANCE_SYSTEM_PROMPT` 通篇「金融合规/监管」。
- **PII 脱敏 bug**：`text[:3] + "*"*(len-6) + text[-3:]`，字符串长度为 5–6 时 `len-6 ≤ 0`，星号段为空，`text[:3]+text[-3:]` 反而暴露/重叠原文。
- `bank_card` 正则 `\d{16,19}` 过于贪婪，会误命中长数字串（订单号等）。
- LLM 审查失败时 `degrade to pass`（fail-open），对 PII 场景有风险，应至少保留规则引擎结论。
- 无合规决策审计日志。

---

## 3. 分阶段优化计划

### 阶段 0 — 解除构建阻断（P0，必须先做）

目标：让 6 个失败测试恢复收集，回到绿色基线。

1. 创建 `agents/intent_models.py`，严格按 Task 1 契约：
   - 枚举 + Pydantic 2 schema
   - `build_intent_decision` 五步：校验 → 按 `SECONDARY_RULES` 重算 agent → 主意图/候选合并 → 安全优先级选主 → 候选去重降序限 3
   - 实体 `extra="forbid"` + 值 ≤128 字符
2. 创建 `tests/test_intent_models.py`（覆盖 agent 重算、安全候选胜出、越界置信度、未知实体拒绝）。
3. 运行 `pytest -q` 确认测试恢复，0 collection error。

**验收**：全量测试可收集，intent 相关测试通过。

### 阶段 1 — 完成意图路由闭环（P0）

1. Supervisor 实现 `build_clarification_message(intent_info)`：按候选区分「政策/申请 vs 订单/维修 vs 账户」三类澄清；通用兜底用 Apple 售后措辞，剔除金融词。
2. 实现安全意图处置节点：为 `account_security / fraud_report / sensitive_data / prohibited_request` 写确定性指引（不得声称已冻结/已处理），写入 `sub_results["security_guidance"]`，`synthesize_response` 优先输出。
3. 修正 `intent_router_node` 硬编码澄清文案（去掉「开户」）。

**验收**：`test_supervisor.py` 全绿（安全指引断言、澄清无金融词）。

### 阶段 2 — 三个下游 Agent 领域对齐（P1）

按领域逐个 Apple 化，每步先跑对应测试：

- **RAG**：重写 `RAG_SYSTEM_PROMPT` 为 Apple 售后；删除死分支，改用新二级意图（`repair_warranty_policy` 等）做 query 增强；rerank 增加分数兜底与引用去重；接入 `min_score`。
- **工单**：`TICKET_SYSTEM_PROMPT` 工单类型改 Apple 售后（维修/退货/订阅取消/投诉/转人工）；订单判定改用实体字段而非 `startswith`；MCP 失败显式降级提示；工单类型枚举校验。
- **合规**：`FORBIDDEN_TERMS` / prompt 改为 Apple 售后适用项；修复 PII 脱敏长度 bug；收紧 `bank_card` 正则边界；LLM 失败时保留规则引擎结论（不无条件 fail-open）；补合规决策审计日志（不记原始敏感值）。
- 同步更新 `mcp/mcp_server.py` 工具桩为 Apple 域 mock 数据。
- 完成后移除 `conftest.py` 的旧→新兼容映射（届时不再需要桥接）。

### 阶段 3 — 实体 TTL 与配置化（P1）

1. `working_memory.py` 加版本化实体状态（`{value, confirmed_turn}`）+ `merge_entities` / `get_active_entities(ttl_turns)`，对下游仍导出扁平 `accumulated_entities`。
2. `api/settings.py` 增加并校验 6 个 `INTENT_*` 配置（阈值 0–1、轮数正整数），`IntentRouterAgent` 从 settings 注入：
   - `intent_confidence_threshold: float = 0.70`
   - `intent_candidate_margin: float = 0.15`
   - `intent_context_turns: int = 3`
   - `intent_entity_ttl_turns: int = 5`
   - `intent_format_repair_enabled: bool = True`
   - `intent_prompt_version: str = "apple-support-v1"`

### 阶段 4 — 离线评测与质量护栏（P2）

1. `evaluation/intent_routing_cases.jsonl`（≥50 条，覆盖 16 个二级意图各 ≥2 条 + 咨询/办理最小对 + 多意图 + 安全样本）。
2. `scripts/evaluate_intent_routing.py`（纯函数指标：accuracy / macro-F1 / 安全召回 / 澄清准确率 / 实体 exact-match）+ `tests/test_evaluate_intent_routing.py`。
3. 更新 README/.env 领域说明与评测命令，运行全量回归 + 真实烟测 5 条用例：
   1. `AppleCare 可以取消吗？` → `knowledge_rag`
   2. `帮我取消 AppleCare` → `ticket_handler`
   3. `Apple 账户被盗并收到可疑验证码` → `compliance_checker` 且返回安全指引
   4. `这个怎么办？` → Apple 售后澄清且不含「开户」
   5. `查订单并申请退款` → 主意图为 `refund_request`

---

## 4. 执行顺序建议

- **阶段 0 → 1**：硬前提。不做，项目连测试都跑不起来。
- **阶段 2**：四个核心领域工程化的主体。
- **阶段 3 → 4**：质量加固。

| 优先级 | 阶段 | 阻断性 |
|---|---|---|
| P0 | 阶段 0：解除构建阻断 | 是（构建/测试无法运行） |
| P0 | 阶段 1：意图路由闭环 | 是（Task 3 测试失败） |
| P1 | 阶段 2：下游 Agent 领域对齐 | 否（功能割裂，靠桥接维持） |
| P1 | 阶段 3：实体 TTL + 配置化 | 否 |
| P2 | 阶段 4：离线评测护栏 | 否 |
