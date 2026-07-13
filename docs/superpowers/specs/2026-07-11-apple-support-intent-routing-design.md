# Apple 售后客服意图路由优化设计

## 1. 背景

SmartCS 当前意图路由主要依赖模型返回自由 JSON，业务提示词仍混有金融、通用电商和 Apple 售后语义。知识库已经以 Apple 官方支持资料为主，因此本轮首先统一业务领域，提升意图分类、实体提取、低置信度澄清和跨轮路由的稳定性。

本设计是当前核心模块优化的第一阶段。完成后依次优化 RAG、工具调用和合规检查。登录验证与角色授权已延期到下一轮。

## 2. 目标

1. 以 Apple 售后客服为统一业务领域。
2. 保持 `knowledge_rag`、`ticket_handler`、`compliance_checker` 三个执行目标。
3. 使用严格结构化输出替代对任意 JSON 的信任。
4. 由服务端确定性计算执行目标，不信任模型自报路由。
5. 区分知识咨询、业务办理、状态查询与安全风险。
6. 识别主次意图和候选列表，首版只执行主意图。
7. 建立格式修复、规则降级和针对性澄清机制。
8. 规范跨轮上下文和实体生命周期。
9. 建立可持续运行的意图路由离线评测集。

## 3. 非目标

本阶段不实现：

- 新增独立业务 Agent；
- 同一轮串行执行多个 Agent；
- RAG 检索和生成逻辑优化；
- 工具内部调用可靠性优化；
- 合规规则和执行前拦截优化；
- 训练专用意图分类模型；
- 客户端认证和角色授权。

## 4. 领域模型

### 4.1 执行目标

| 执行目标 | 适用范围 | 二级意图 |
|---|---|---|
| `knowledge_rag` | 说明、政策与操作指南查询 | `product_support`、`repair_warranty_policy`、`return_refund_policy`、`subscription_policy`、`account_guidance`、`sales_policy` |
| `ticket_handler` | 状态查询或需要业务系统执行的动作 | `order_query`、`refund_request`、`repair_request`、`subscription_cancel_request`、`complaint`、`human_escalation` |
| `compliance_checker` | 账户安全、欺诈、敏感数据或禁止请求 | `account_security`、`fraud_report`、`sensitive_data`、`prohibited_request` |

### 4.2 一级意图

一级意图为：

- `consultation`：知识、政策和指南咨询；
- `query`：订单、工单或业务状态查询；
- `action`：要求系统执行退款、维修、取消订阅等动作；
- `complaint`：投诉或明确要求转人工；
- `security`：账户安全、欺诈、敏感信息和禁止请求；
- `unknown`：信息不足或无法归类。

一级与二级意图的组合由服务端维护允许映射，非法组合不能进入下游 Agent。

### 4.3 咨询与办理

最小语义对必须路由不同：

- “AppleCare 能否取消？” → `consultation/subscription_policy/knowledge_rag`
- “帮我取消 AppleCare” → `action/subscription_cancel_request/ticket_handler`
- “如何恢复 Apple 账户？” → `consultation/account_guidance/knowledge_rag`
- “我的 Apple 账户被盗了” → `security/account_security/compliance_checker`

### 4.4 多意图

结构化结果保留主意图和最多三个候选，但首版只执行主意图。

优先级为：

```text
安全/欺诈 > 明确业务动作 > 状态查询 > 知识咨询
```

示例：

- “我的账户被盗了，而且想查订单”优先进入安全处理。
- “查订单并申请退款”优先处理退款动作，并允许最终回复提示用户后续继续查询订单。

该优先级是确定性服务端规则，不由模型自由决定。

## 5. 结构化结果

新增严格的路由结果模型：

```text
IntentDecision
├── primary_intent: enum
├── secondary_intent: enum
├── confidence: float [0, 1]
├── suggested_agent: enum
├── entities: IntentEntities
├── candidates: list[IntentCandidate], max=3
└── reason_code: enum
```

`IntentCandidate` 包含一级意图、二级意图和置信度。候选按置信度降序排列，不允许重复。

`reason_code` 只允许短枚举，例如：

- `explicit_policy_question`
- `explicit_action_request`
- `explicit_status_query`
- `security_risk_detected`
- `context_follow_up`
- `ambiguous_request`
- `parser_fallback`

不保存或要求模型输出 chain-of-thought。

## 6. 实体模型

允许的实体键：

- `order_id`
- `ticket_id`
- `product`
- `device_model`
- `subscription`
- `account_issue`
- `region`

规则：

1. 实体值必须是短字符串，限制最大长度。
2. 丢弃空值、嵌套对象和白名单外字段。
3. `order_id`、`ticket_id` 使用领域格式校验。
4. 设备型号、订阅名称和地区执行规范化，但不虚构缺失值。
5. 模型不能通过实体字段注入新的 Agent、Prompt 或工具参数。

## 7. 路由计算

模型负责语义分类，服务端负责最终路由。

服务端维护不可绕过的映射：

```text
secondary_intent -> primary_intent -> suggested_agent
```

处理步骤：

1. 验证一级和二级意图是否合法；
2. 验证组合是否存在于 taxonomy；
3. 根据二级意图重新计算一级意图和 Agent；
4. 应用多意图安全优先级；
5. 应用低置信度和候选差距阈值；
6. 生成最终 `IntentDecision`。

即使模型返回 `suggested_agent`，服务端也会覆盖该值。模型自报目标仅用于诊断，不作为执行依据。

## 8. 模型输出与解析流程

采用以下流程：

1. 使用结构化 Prompt 请求 JSON 对象。
2. 使用 Pydantic 模型验证类型、枚举、范围、列表长度和实体。
3. 首次验证失败时进行一次“仅修复格式”的模型重试。
4. 第二次仍失败时进入确定性规则降级。
5. 记录解析成功、格式修复或规则降级状态，但不记录原始敏感消息。

格式修复请求只携带所需 schema 与待修复结果，不引入新的业务判断；修复后的结果仍需完整验证。

## 9. 高确定性降级规则

降级规则不是主分类器，仅在结构化解析彻底失败时使用。

规则顺序：

1. 账户被盗、诈骗、泄漏、敏感凭据或明显禁止请求 → `compliance_checker`；
2. 明确订单号/工单号和查询词 → `ticket_handler`；
3. 明确“申请、取消、维修、退款、投诉、转人工”等动作 → `ticket_handler`；
4. 其他问题 → `knowledge_rag` 候选，但标记低置信度；
5. 信息不足时进入澄清，不直接调用下游 Agent。

规则词表按 Apple 售后领域维护，并有独立单元测试。

## 10. 低置信度和澄清

进入澄清的条件：

- 最终置信度低于配置阈值；
- 第一和第二候选的置信度差低于配置阈值；
- 一级/二级意图组合无法修复；
- 业务动作缺少不可缺少的对象，且无法从有效上下文补全。

澄清问题必须针对候选生成。例如：

- 政策咨询与退款办理之间不明确：询问用户是了解退款规则还是立即申请退款；
- 订单查询与维修申请之间不明确：询问希望查询订单还是为设备发起维修；
- 单纯“这个怎么办”：结合上一轮意图和实体询问具体目标。

阈值配置化，并由离线评测集校准。未测量前不宣称固定阈值适合生产。

## 11. 跨轮上下文

分类模型只接收必要上下文：

- 当前用户消息；
- 最近三轮经过角色过滤的对话摘要；
- 上一轮规范化意图；
- 白名单累积实体；
- 当前待澄清候选。

不把内部工作记忆快照、工具原始响应或无界完整历史放入意图 Prompt。

### 11.1 实体生命周期

- 本轮明确实体覆盖同类型旧值；
- 不同类型实体互不覆盖；
- 用户明确纠正时清除或替换旧值；
- 实体记录最后确认轮次；
- 超过配置轮次 TTL 的实体不再自动补全；
- 安全意图结束后不把敏感实体传播到普通咨询。

工作记忆的 `_wm_context` 扩展为版本化、规范化上下文，不直接保存模型任意输出。

## 12. 配置

新增配置项：

```env
INTENT_CONFIDENCE_THRESHOLD=0.70
INTENT_CANDIDATE_MARGIN=0.15
INTENT_CONTEXT_TURNS=3
INTENT_ENTITY_TTL_TURNS=5
INTENT_FORMAT_REPAIR_ENABLED=true
INTENT_PROMPT_VERSION=apple-support-v1
```

默认值用于开发和建立基线，生产值必须通过评测校准。

## 13. 可观测性

记录以下非敏感指标：

- 一级、二级意图和最终 Agent 分布；
- 平均置信度；
- 澄清率；
- 首次解析失败率；
- 格式修复成功率；
- 规则降级率；
- 多意图比例；
- 路由耗时和模型 token；
- Prompt、模型和 taxonomy 版本。

日志和 trace 不记录完整用户消息、模型原始推理或未脱敏实体。

## 14. 测试与评测

### 14.1 单元测试

覆盖：

- 每个合法一级/二级组合；
- 非法枚举、越界置信度、重复候选；
- 白名单外实体和超长实体；
- 咨询/办理最小对；
- 多意图优先级；
- 格式修复成功与失败；
- 每条降级规则；
- 低置信度和候选差距澄清；
- 实体覆盖、纠正和 TTL。

### 14.2 Golden dataset

第一版数据集覆盖：

- 每个二级意图的明确表达及同义表达；
- 知识咨询与业务办理最小对；
- 多意图消息；
- 指代、追问和上下文补全；
- 低信息消息；
- 安全高风险消息；
- 无效模型输出、枚举越界和恶意结构化内容；
- 容易混淆的退款政策/退款申请、订阅政策/取消订阅、账户指南/账户安全样本。

数据集使用固定 JSONL/JSON 格式，包含输入、必要上下文、预期意图、预期 Agent、允许实体和是否应澄清。

### 14.3 指标

- 一级意图 macro-F1；
- 二级意图 macro-F1；
- Agent 路由准确率；
- 安全高风险意图召回率；
- 实体 exact/partial match；
- 应澄清样本准确率；
- 解析失败率；
- 格式修复率；
- 规则降级率。

首次评测建立基线。之后根据业务风险设置回归门禁，高风险安全召回优先于总体准确率。任何 Prompt、模型或 taxonomy 修改必须重新运行离线评测。

## 15. 实施边界

本阶段修改范围限定为：

- 意图 taxonomy 和结构化 schema；
- `IntentRouterAgent`；
- Supervisor 的路由与澄清逻辑；
- 工作记忆中的规范化意图/实体上下文；
- 意图路由单元测试和离线评测脚本；
- 相关配置与文档。

不在本阶段顺带重构 RAG、工具和合规模块。路由优化通过测试和基线评测后，再进入 RAG 阶段。

## 16. 验收标准

1. 金融领域意图和提示词从路由主流程移除，统一为 Apple 售后语义。
2. 模型输出必须经过严格 schema 验证。
3. 服务端确定性计算 Agent，模型不能通过返回值绕过映射。
4. 咨询与办理最小对路由正确。
5. 多意图按确定性优先级选择主意图，并保留候选。
6. 无效模型输出经过最多一次格式修复，仍失败后安全降级或澄清。
7. 低置信度和候选接近时不会盲目调用下游 Agent。
8. 跨轮上下文只包含受控历史、规范化意图和白名单实体。
9. 实体支持覆盖、纠正和轮次 TTL。
10. Golden dataset、评测脚本和回归测试可重复运行。
11. 现有相关测试经更新后全部通过，不破坏 RAG、工具、合规和 TUI 的既有回归测试。