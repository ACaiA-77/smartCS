# SmartCS 简历与面试说明

这是一份内部项目介绍材料，不是虚构的求职申请。下面的表述只覆盖当前源码和本地确定性验证，候选人应根据自己的实际参与范围选择动词，不把团队成果改写成个人独立完成。

2026-09-20 已完成本地收口：认证阶段全量 pytest 351 项，认证专项 63 项、checkpoint 专项 14 项分别复验通过；RAG runtime isolation 收尾后最终默认回归为 `367 passed, 18 skipped`，另有 Eval 14 / 14、Node 15 / 15、两个真实进程崩溃窗口及独立 HTTP / Chrome 验收。RAG runtime closure 已通过并冻结。完整认证结论见 [执行报告](../artifacts/auth_20260919/execution_report.md)。284 项 pytest、Eval 14 / 14 和 3 项 Node 测试仍保留为 2026-09-18 checkpoint 阶段的历史基线，不与后续数量相加。

## 简历要点候选

以下五项基于已有实现和本地验证，不包含用户量、QPS、延迟提升、成本节省或生产业务结果。写入个人简历前，需按实际参与范围选择职责动词，不直接加上“独立完成”或“主导”；本地验收通过不等于生产部署完成。

1. **受控编排：** 基于 FastAPI 和显式异步编排拆分意图路由、响应分支和业务执行，让客服请求最多进入一个自然对话或业务处理分支，业务写入仍需通过统一执行策略。
2. **业务幂等：** 针对退款和工单重复提交、提交结果不明的问题，结合执行账本、固定幂等键和 SQLite 权威业务查询，实现受限的执行回放与崩溃恢复，验证已有业务效果不会再次创建。
3. **状态恢复：** 针对短期会话缓存无法保存工作流进度的问题，以 MySQL 持久化节点快照和请求回执，通过版本比较及会话连接锁控制更新；进程重启后可恢复待确认退款和未完成节点，不能代替用户确认或保证任意外部写入 exactly-once。
4. **验证与观测：** 以离线确定性 Eval、故障注入和请求、工具、恢复指标检查路由及副作用边界；认证阶段全量 `pytest` 为 351 项通过，RAG isolation 收尾后最终为 `367 passed, 18 skipped`，另有 Eval 14 / 14、Node 15 / 15，真实进程回归覆盖两个写入崩溃窗口；不代表线上模型质量或生产 SLA。
5. **双域 RAG：** 以 Apple 支持和 Agent 工程双域 corpus 构建 Dense + BM25 + RRF + Cross-Encoder 检索，固定 60 条 query/qrels 的 global Top-10 `hybrid_rerank` 达到 Recall 0.825、MRR 0.800、nDCG 0.749；生产 runtime 与测试/Eval 依赖隔离已冻结，不把本地 benchmark 当作线上质量。
6. **身份与数据隔离：** 针对客户端可伪造用户 ID 的问题，基于 JWT 与数据库账号状态建立可信身份，将认证上下文注入 Agent 工作流，在会话、checkpoint、订单、退款和工单链路再次核对归属；不允许模型参数改写业务身份，认证、会话权限和业务隔离专项 63 项通过，不扩大为 RBAC 或企业 IAM。

## 30 秒项目介绍

这是一个本地多 Agent 客服项目，Agent 负责理解意图，退款和工单仍由业务域校验并写入 SQLite。项目通过 MySQL 保存节点进度和待确认状态，结合执行账本处理重启恢复；RAG 使用 Apple 支持和 Agent 工程双域检索。JWT 登录和账号所属会话由后端认证身份约束。认证阶段全量 `pytest` 351 项通过，RAG runtime isolation 收尾后最终为 `367 passed, 18 skipped`；真实进程恢复和 HTTP / Chrome 登录、刷新及账号隔离也通过了本地验收。

## 主张、来源和边界

| 可表达主张 | 代码或验证来源 | 个人边界 |
| --- | --- | --- |
| 显式 async 编排和最多一个业务 Handler | `agents/orchestrator.py` | 可以说明项目实现了这条链路，不自动等于个人独立设计全部架构 |
| 工具执行确认、幂等、READ 重试和写入不自动重试 | `mcp/tool_execution.py`、`mcp/execution_ledger.py` | 说明已实现的执行策略，不声称覆盖任意外部系统 |
| 退款、工单持久化和受限恢复 | `refunds/service.py`、`tickets/service.py`、`mcp/execution_recovery.py` | 只覆盖当前两个支持恢复的写工具 |
| MySQL 节点持久化、请求回放与并发保护 | `checkpoint/models.py`、`checkpoint/store.py`、`agents/orchestrator.py`；`tests/test_checkpoint.py` | 不等同于跨库事务、多实例生产部署或任意工具 exactly-once |
| 认证阶段全量 pytest 351 项、最终全量 367 项、认证专项 63 项、Node 15 / 15 | [full_suite.txt](../artifacts/auth_20260919/full_suite.txt)：`351 passed in 71.72s`；最终 RAG isolation 回归：`367 passed, 18 skipped`；[auth_tests.txt](../artifacts/auth_20260919/auth_tests.txt)：`63 passed in 28.67s`；Node 结果由主 Agent 验证并汇总至 [执行报告](../artifacts/auth_20260919/execution_report.md) | 63 项包含在最终 367 项内；Node 另计，真实浏览器验收另有独立证据 |
| 本轮 checkpoint 专项 14 项 | [checkpoint_tests.txt](../artifacts/auth_20260919/checkpoint_tests.txt)：`14 passed in 21.34s` | 已包含在最终全量 367 项内，不额外累加 |
| RAG runtime isolation 与双域 benchmark | `367 passed, 18 skipped`；Eval 14 / 14；`hybrid_rerank` global Top-10 Recall 0.825、MRR 0.800、nDCG 0.749、wrong-domain rate 0.063 | `artifacts/smartcs_final_20260920/`、`artifacts/rag_round3/metrics.json`；固定 60 条 query/qrels，本地结果不代表线上质量 |
| 本轮两个真实进程崩溃窗口恢复通过 | [checkpoint_process.json](../artifacts/auth_20260919/checkpoint_process.json) | 两个场景各保留 1 条退款，最终 replay 的 LLM 调用为 0；不表示恢复全过程不调用模型 |
| 本轮独立 HTTP / Chrome 本地验收通过 | [browser_restart.json](../artifacts/auth_20260919/browser_restart.json)；真实 JWT / MySQL，进程 26244 重启为 4240 | 账号 B 读取、恢复、删除 A 的会话均为 404；本人订单、刷新、丢失 POST 后继续、logout 清空和账号切换隔离均通过，使用确定性模型及隔离 SQLite，不是生产验证 |
| 2026-09-18 历史基线：284 项全量 pytest、原有 Eval 14 / 14、3 项 Node 测试 | 验收报告：[checkpoint_test_report.md](../artifacts/checkpoint_20260918/checkpoint_test_report.md)；最终全量原始输出：[full_suite.txt](../artifacts/checkpoint_20260918/full_suite.txt)，结果为 `284 passed in 58.14s`；其他验收记录见同目录 `round1_acceptance.md`、`baseline.md` | 14 项 checkpoint pytest 已包含在 284 项内；3 项 Node 另计，不代表本轮认证验收通过，原有 Eval 没有扩展成 checkpoint 场景集 |
| 真实进程与浏览器恢复 | `scripts/verify_checkpoint_restart.py`、`scripts/verify_checkpoint_http.py`；`artifacts/checkpoint_20260918/browser_restart.json` | 使用真实 MySQL、隔离业务 SQLite 和确定性测试模型，不是生产故障统计 |
| 历史 253 项测试和 14 / 14 个 Eval 场景 | 2026-09-18 的本地忽略证据 `artifacts/c2c_ci_repository_readiness_01/iteration_1/full_suite.txt`、`eval_json.txt` | 仅保留为早期验收证据，不再作为当前测试数量 |
| 运行时请求、工具和恢复指标 | `tracing/observability.py`、`api/main.py` | 只说明 runtime operational logs 和聚合指标，不代表项目所有日志都已统一治理 |
| JWT 与账号状态构成可信业务身份 | `auth/jwt.py`、`auth/password.py`、`auth/context.py`、`auth/dependency.py`、`api/main.py`；[认证专项结果](../artifacts/auth_20260919/auth_tests.txt) | logout 只清 cookie，不撤销已复制的 JWT，不是企业 IAM |
| 账号会话持久化与业务归属检查 | `platform_db/database.py`、`platform_db/sessions.py`、`mcp/order_repository.py`、`mcp/mcp_server.py`、`mcp/tool_execution.py` | MySQL 保存平台账号和会话，SQLite 业务表不迁移；测试结论以 `artifacts/auth_20260919/` 为准 |

不要把以上材料扩展成用户规模、真实退款量、生产流量、SLA、竞品排名或业务收益。若被问到个人职责，回答实际负责的模块，并把其余部分明确为项目已有能力或协作成果。

## 面试叙事主线

### 为什么使用显式 Orchestrator，而不是 LangGraph？

当前目标是把一条客服请求的路由边界、业务 Handler 数量和执行入口讲清楚。显式 `ChatOrchestrator` 让这条路径可以直接阅读和测试，也避免为了一个明确流程引入没有必要的图运行时。LangGraph 可以是其他场景的选择，但不是当前本地目标的必需依赖。

### 为什么 Agent 不拥有业务状态？

模型输出适合做意图判断和交互表达，不适合直接成为退款或工单的权威结果。业务域会再次检查订单归属、退款条件、工单身份和 payload hash，并在 SQLite 事务中决定最终状态。这样，模型重复、误判或输出格式变化不会直接绕过业务约束。

### `idempotency_key` 和 `client_request_id` 有什么区别？

`idempotency_key` 属于执行层，用来判断一次相同的工具执行是否已经完成，结果保存在 `ExecutionLedger`。聊天 API 的 `client_request_id` 标识一轮请求，MySQL 保存回执；工单工具也有自己的 `client_request_id`，标识业务创建请求，两者不是同一个作用域。工单还会比较规范化 payload hash，同一请求 replay，不同内容返回冲突。

### 为什么 READ 可以重试，WRITE 不能自动重试？

READ 没有业务写入副作用，传输超时后有限重试不会凭空创建新的业务效果。WRITE 的超时不能证明服务端没有提交，如果立即重试可能产生重复退款或重复工单。因此 WRITE 只执行一次，并依赖 ledger、业务域查询和受限恢复来处理不确定窗口。

### 如果 SQLite 已提交，但 ledger 还没完成，系统怎么办？

进程重启时会扫描达到 stale threshold 的 `in_progress` claim，默认阈值是 60 秒，单次最多扫描 100 条。显式恢复还会对当前请求保存的幂等键进行相同阈值的受限协调。对 `refund_create`，系统按订单和用户查找已有退款效果；对 `ticket_create`，系统按业务请求 ID、用户和 payload hash 查找。找到匹配效果就条件完成 stale claim，没有效果时在支持的路径上条件释放。这个恢复只覆盖退款和工单，不是任意写工具的全局 exactly-once 保证。

### 工单恢复如何防止 payload 身份不一致？

ledger 的工单 recovery payload 只保留 `client_request_id`、`user_id` 和 `request_payload_hash`。恢复必须查到同一用户的请求，并且 hash 一致。已有工单属于该用户但 hash 不一致时，结果是 terminal business conflict，不返回旧工单号，也不自动重试。用户不匹配时，带用户条件的查询为空，走无效果的条件释放路径。

### 如何防止跨用户工单泄漏？

我会先说明身份来源，再说归属条件。客户请求的业务用户 ID 来自 JWT 校验后重新读取的平台账号，不是浏览器或模型随意填写的参数。工单查询同时匹配工单号和这一业务身份，只返回允许的字段；创建请求 ID 冲突不会泄露已有工单的编号、标题或状态。工具入口还会拒绝与认证身份不一致的参数，包括尝试复用旧执行账本的请求。本轮增加的是用户级隔离，不是 RBAC，具体跨账号测试结果仍以本轮验收材料为准。

### 为什么有 JWT 后，还要查询账号并检查会话归属？

我把这三步看作不同问题：JWT 证明凭据由服务端签发且没有过期，数据库查询确认账号现在仍有效，会话归属检查确认这次请求能访问指定会话。JWT 的主体字段只标识平台账号，服务端每次都加载活跃账号，再通过请求内认证上下文传入业务身份。会话表保存平台账号 ID，checkpoint 又保留业务用户归属校验，所以“有一个有效 JWT”不等于“能读取任意会话”。账号与业务身份的映射、会话归属均由后端维护，不能由前端自行绑定；本轮也没有公开注册入口。

### 模型生成了别人的用户 ID，或者命中了旧账本，会怎么办？

我不会依赖提示词要求模型自觉遵守权限。订单、退款、工单的两个工具入口共用身份检查：参数里没有业务身份时补入后端认证身份，明确填了另一个身份时直接拒绝。执行器把这个检查放在保存写入计划和回放执行账本之前，因此即使攻击者知道别人的幂等键，也不能靠直接返回旧结果绕过检查。订单详情和列表按当前用户过滤；退款在业务层核对订单归属后才写入，工单冲突不返回他人 metadata。这些边界已纳入本轮认证、会话权限和业务隔离专项，合计 63 项通过；不将其扩大为任意工具的权限保证。

### 客户能直接调用写工具或审批接口吗？

不能。客户的两个通用工具 HTTP 入口只允许订单查询、退款评估、工单查询和知识检索这四个 READ 工具，创建退款和工单必须经过聊天与既有执行策略。客户传入确认标志，也不能把通用入口变成任意写工具入口。审批路由和含原始工具调用明细的指标路由拒绝客户访问，内部审批服务仍保留给执行策略和离线测试。这里没有管理员账号、角色层级或管理后台，所以我会称它为客户访问边界，不会包装成完整 RBAC。

### HttpOnly cookie 和退出登录分别保证什么？

我会先讲清范围：登录把短期 JWT 放进名为 `smartcs_auth` 的 HttpOnly cookie，页面 JavaScript 不需要读取 token；写请求还检查来源，CORS 默认同源，跨源开发使用精确白名单。HttpOnly 不等于能撤销已经复制的 token。当前退出登录只清除浏览器 cookie，没有撤销表或 refresh-token 轮换；已复制的 token 在最多 30 分钟有效期内仍可能使用。账号停用会在后续请求查询账号时生效。本轮不把这些机制描述为 SSO、企业 IAM 或生产零信任。

### 为什么需要确定性 Eval，而不只写单元测试？

单元测试适合验证函数和组件合同，Eval 则把路由、会话、工具执行和业务域串成场景，检查“没有确认就没有副作用”“一次请求只产生一个效果”等跨组件不变量。故障注入还可以检查 READ 有界重试、WRITE 不自动重试和失败收敛。崩溃恢复不属于这 14 个 Eval 场景，具体契约由 `tests/test_execution_recovery.py` 等测试覆盖。

### 为什么不在这里使用 LLM-as-judge？

本阶段要验证的是可重复的业务状态和副作用边界。LLM-as-judge 自身会引入模型波动，无法替代对 SQLite 记录、ledger 状态和调用次数的确定性断言。真实模型回答质量需要另外准备数据集和评价协议。

### 14 / 14 的 Eval 数字证明什么？

它证明 2026-09-18 的 Deterministic LLM、本地 SQLite Sandbox 和故障注入验收中，14 个场景的既定不变量全部通过，记录的路由、副作用安全和故障收敛指标为 1.0。这是历史本地确定性验证，不包含本轮新增的登录和跨账号隔离验收。

### 14 / 14 不证明什么？

它不证明在线 LLM 的开放式回答质量、真实用户准确率、生产流量、SLA、部署规模、竞品表现或远程 GitHub Actions 已经执行。它也不替代本轮认证验收、分布式写协调和真实模型数据集。

### 当前有哪些可观测性？

外层 ASGI 包装器在 FastAPI handler 前设置 `request_id ContextVar`，并返回 `X-Request-ID`。`InstrumentedToolExecutor` 记录工具状态、风险级别、尝试次数、replay、超时和耗时，`RuntimeMetrics` 汇总 requests、tools、recovery 三类指标，启动恢复也会计数。当前运行时操作日志只输出清洗后的字段，不包含工具 arguments、result 或自由文本业务内容，不能把这句话扩大为所有项目日志。

### 如果进入真实生产环境，会先改什么？

当前认证、跨账号隔离及 HTTP / Chrome 已通过本地验收，进入生产前仍需按实际部署需要验证 token 撤销、账号管理、授权粒度和多实例业务存储。当前 checkpoint 已使用 MySQL 会话连接锁和版本检查，但本地 SQLite 不构成跨主机共享业务存储，也没有跨库事务或 RBAC，不能称为多实例生产 ready。在线模型质量仍需单独的数据集和评价协议；远程 CI 执行没有在本次工作会话中验证。

## Checkpoint Recovery 问答

### 为什么有会话记忆，还需要 checkpoint？

我会先区分“记得聊过什么”和“知道执行到哪里”。原来的短期记忆保存聊天和待确认状态，但内存降级会在重启后丢失，Redis 也有过期时间，更没有记录回答是否生成、合规是否完成。现在 MySQL 保存已完成节点的状态，恢复时跳过这些节点，再执行尚未完成的步骤；消息最多保留最近 20 条，不是永久聊天档案。模型生成到一半时需要重跑生成步骤，不能把这项能力说成恢复模型内部思考或逐 token 续传。

### Checkpoint 和 ExecutionLedger 为什么不能合并理解？

我把 checkpoint 理解为请求进度，把执行账本理解为一次工具写入的凭据。前者保存意图、节点结果、固定工具参数、会话上下文和最终回答，后者记录幂等键、参数摘要及执行结果。比如模型已生成回答但还没审查，只需恢复审查节点；退款已提交却还没保存最终回答，则必须先依靠账本和业务记录确认效果，再继续回答。MySQL 与业务 SQLite 没有共同事务，所以两层记录仍需协调，不能把保存快照当成防止重复退款的充分条件。

### 两个关键崩溃窗口分别怎么恢复？

第一个窗口是业务 SQLite 已提交、执行账本也已完成，但 checkpoint 尚未推进。我会沿快照中固定的参数和幂等键恢复，由账本回放结果，不再次调用业务写工具。第二个窗口是业务已提交、账本仍为执行中，这时不能因为没收到结果就重写；只有达到默认 60 秒阈值后，才通过已有权威查询协调退款或工单记录。证据不足或不支持恢复的写工具需要人工处理。2026-09-18 的历史验收和 2026-09-19 的真实进程回归都覆盖这两个窗口；本轮各场景退款始终 1 条，最终同请求 replay 的 LLM 调用为 0。恢复阶段仍可能调用后续模型节点，这不是对任意外部系统的 exactly-once 保证。

### WAIT_CONFIRM 重启后会自动退款吗？

不会。我会把待确认状态和执行授权分开说明：MySQL 持久化的是待确认订单、金额和固定参数，重启后的显式恢复只回放确认提示，不自动把它当成用户同意。用户必须用新的聊天请求明确确认或取消；取消结果持久化后，不会再从旧 Redis 数据恢复待确认动作。同一请求 ID 可以回放已有结果，不同 ID 才代表新一轮。本轮认证改造增加账号所属会话索引，刷新或重新登录后仍须验证会话归属，不能通过更换客户端用户参数恢复他人的待确认退款。独立 HTTP / Chrome 验收确认待确认状态不会自动执行，确认加 replay 仅有 1 条退款，浏览器刷新与账号切换隔离也通过。

### 补断点恢复时，为什么仍然没有引入 LangGraph？

我会从现有流程和本次范围解释选择，而不是说某个框架没有价值。项目已有显式异步编排器，一轮请求的路由、响应分支、合规检查和合成步骤都很明确；这次需要补的是节点状态落盘、固定写参数和请求恢复，因此保留原链路，在边界接入持久化即可。改成图运行时会同时迁移已有业务链路，但不会代替业务幂等、确认或权威结果查询。当前实现没有通用任务引擎、历史分支回放或逐 token 恢复，也没有证据证明它在性能上优于 LangGraph。

### MySQL 乐观锁和连接锁各自解决什么？能支持多实例了吗？

我会把两个机制分开：版本比较解决“旧快照覆盖新状态”，更新必须同时匹配会话、用户和旧版本；会话连接锁解决“两个请求同时开始执行”，同一会话只能由持有 MySQL 锁连接的请求推进。连接释放后锁随之释放，保存前还会验证锁归属，不按超时抢走仍在运行的请求。但这些机制没有把业务 SQLite 变成跨主机共享存储，也没有提供 MySQL 与 SQLite 的分布式事务。它们证明的是当前本地链路的并发保护，不能直接推导为多实例生产 ready。

## 5 分钟演示脚本

以下是项目演示流程；已完成的独立 HTTP / Chrome 本地验收记录见 [browser_restart.json](../artifacts/auth_20260919/browser_restart.json)，不以演示步骤替代原始证据。使用登录后的 Web 或保存 cookie 的 HTTP 客户端；旧 TUI 尚未适配，匿名协议会被拒绝。

1. 用一张架构图说明 `JWT -> active account -> UserContext -> session ownership -> ChatOrchestrator`，再指出 `KnowledgeRAGAgent -> HybridRetriever -> Dense FAISS + Sparse BM25 -> RRF -> Cross-Encoder`，退款和工单经过身份检查及 `ToolExecutor`。
2. 登录账号 A，由服务端创建会话，查询“我的订单”，展示订单 READ 路径和归属过滤；不在请求中设置 `user_id`。
3. 使用同一用户、同一会话和一笔符合条件的订单发起退款请求，展示 `refund_evaluate` 的 eligibility 结果与会话 `pending_action`。
4. 回复“确认退款”，展示 `refund_create`、ledger complete、待确认状态清除，以及 SQLite 中一个退款效果。重复确认时说明没有待确认动作；相同执行键重放则由 ledger 返回已保存结果。
5. 创建并查询支持工单，展示 `client_request_id`、payload hash 和按用户过滤的查询。
6. 刷新后恢复本人会话，再退出并登录账号 B，验证不能读取 A 的会话或订单。聚合指标可在登录后查看 `/api/metrics/runtime`，不要使用客户身份访问审批路由或 `/api/metrics`。离线 Eval 摘要用 `python -m evals.runner --json` 查看。

如果外部 LLM provider 不可用，使用下面的 API-free 证据演示，不把它描述成在线聊天质量验证。还可以按需运行已有的确定性业务测试命令：

```powershell
python -m evals.runner
python -m pytest tests/test_business_sandbox.py tests/test_refund_service.py tests/test_ticket_service.py -q
```

上面的业务测试命令只作为演示备选，本阶段不执行。

## Final Project Readiness

以下区分 2026-09-18 的 checkpoint 结项历史、2026-09-19 的认证验收和 2026-09-20 的 RAG runtime 收口；本地通过不代表生产部署完成：

| 范围 | 状态 |
| --- | --- |
| Backend architecture | 原显式编排保留；认证与会话归属层已纳入认证阶段全量 351 项和最终全量 367 项测试 |
| Business sandbox | SQLite 业务表保留；认证、会话权限和业务隔离专项 63 项通过 |
| Write safety / idempotency | 重放前身份检查通过专项回归，两个真实进程崩溃场景未产生重复退款 |
| Checkpoint / crash recovery | 本轮专项 `14 passed in 21.34s`；两个真实进程窗口再次通过，各 1 条退款，最终 replay 的 LLM 调用为 0 |
| Agent Eval / failure injection | Eval 14 / 14；RAG runtime isolation 与认证边界分别由最终全量回归覆盖 |
| Authentication / customer isolation | 专项 `63 passed in 28.67s`，已包含在最终全量 367 项内 |
| Node / HTTP / browser | Node 15 / 15；真实 JWT / MySQL 的独立 HTTP / Chrome 本地验收 PASS |
| Runtime observability | 保留聚合指标；原始明细和审批接口拒绝客户访问 |
| Repository / CI configuration | 已本地提交至 `54aa0a9`；未 push，未验证 remote CI，未 deploy |
| Documentation / presentation | 已统一最终回归、RAG benchmark 指标、历史证据和非生产边界 |

本轮最终结论以 [执行报告](../artifacts/auth_20260919/execution_report.md) 和同目录原始输出为准。当前稳定节点已本地提交至 `54aa0a9`；未 push、未验证 remote CI、未 deploy，也没有据此证明真实生产运行。公共注册、密码找回、OAuth、SSO、RBAC、管理后台、SSE、分布式协调和更大的 live-model Eval 数据集均未纳入本轮。
