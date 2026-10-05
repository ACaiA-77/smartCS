# 交接指令：Phase 10 — 收口修复轮（GPT 收口方案 8 项，已验收采纳）

> **发件方**：Claude Code（规划与验收方） ｜ **日期**：2026-10-04
> **前置**：迁移 Phase 0-9 全部验收通过；GPT 收口方案（`docs/SmartCS_Pi_Harness_Closeout_Review.md`）经逐条源码/运行态验证**全部属实**，用户批准立即执行。
> **注意**：用户正在本地 8000 验收——服务重启要**批量合并、一次完成**，重启前告诉我一声，中断的在途请求由工作台"继续处理"恢复。

---

## 1. 执行项（按此顺序）

### ① P0：Memory Outbox 生产闭环（工作量最大，最先做）
- `main.ts` 装配并启动 `MemoryOutboxDispatcher`（与 AuditDispatcher 同模式，含优雅 stop/flush）
- **身份恢复去内存化**：dispatcher 不再依赖 `identityFor(sessionId)`（TurnContext 会随 turn 结束/重启/驱逐消失）——改为从 durable 状态恢复：receipt（session_id/client_request_id）→ conversation_session（account）→ memory_source_event（business_user_id）→ 现签 Service JWT 投递。核心原则照 GPT：**恢复不得依赖任何 Node 进程内状态**
- 验收（全要有用例）：GPT 列的 5 条（正常 done / enqueue 前 kill -9 重启补投 / idle 驱逐后补投 / 重复投递不产生重复 candidate / 失败只重试记忆不重放 LLM 与工具）**+ 第 6 条：当前 dev 库积压的 18 条 pending 被自然补投清零**（真实生产恢复演练，报告记录补投数与最终计数）

### ② P0：System Prompt 对齐真实工具面
- 重写为 READ（order_query/knowledge_search/ticket_query/refund_evaluate/risk_check）+ WRITE（refund_confirm/ticket_create）两段；写明两段式退款流程与工单 same-turn 语义
- 必含业务原则："Agent 可提出工具调用，但无权自行授权业务写操作；WRITE 是否执行由 Python 确定性授权层决定"；**不得**出现"只能只读"或"你不能声称已完成"这类与 live 事实矛盾的话
- 保留现有的 knowledge_search → MCP 工具名动态替换机制；`SMARTCS_SKILLS=on` 时技能区段逻辑不变

### ③ P1：模型面 Tool Schema 去系统字段（与 ② 同轮做）
- 模型可见 schema 只留业务参数（order_query: order_id；refund_evaluate: order_id, reason?；ticket_query: ticket_id/query；risk_check: action, amount?；ticket_create: title, description, priority?, category?；refund_confirm: pending_action_id）
- **裁决记录**：这是对 Phase 2"逐字段翻译 mcp_server.py"原则的**显式推翻**（模型面 schema = 业务参数子集）；TS 侧加映射注释说明与 Python 端 input_schema 的关系；Python 端零改动（force-bind/注入已就绪）
- 回归重点：P2-3 的未知字段拒绝、P4 幂等回归、Phase 5 全链（身份字段剥离逻辑对"模型不再传"依然成立）

### ④ P1：Production 禁止 Faux 静默降级
- `SMARTCS_PROVIDER_MODE` 显式三态（openai/faux）；**缺省与配置不全时：测试环境（vitest 内）保持现有 faux 回落，进程服务模式 fail-fast 拒启**（区分依据：测试由 fixture 显式传 mode，服务由 env 显式声明——实现方式自选，报告说明）
- 这正是 Phase 9 compose 第三坑的正式收口

### ⑤ P1：RAG 延迟分段计时（分析先行，不急着优化）
- 给检索链路加分段计时（query 改写/dense/BM25/RRF/rerank/序列化/传输），暴露 P50/P95（可先日志级）
- 用计时数据定位瓶颈（重点怀疑：CrossEncoder 每请求推理、top-k→rerank-k 过大）——**只出数据与结论，优化改动另行报批**；不降检索质量

### ⑥ P1：文档收口
- `pi-harness/README.md`（新建：架构/命令/边界）；`python-impl/README.md` 重写为双层架构主链路（ChatOrchestrator 降级为 legacy 路径描述）；`docs/architecture.md` / `runtime-boundaries.md` / `recovery.md` 三份（可适度合并）
- 历史 Phase 报告与方案文档**不删不改**，在 README 标注 "Historical Migration Record"

### ⑦ P2：MCP Compose 演示入口
- compose 加 profile（如 `--profile mcp`）：拉起 Python MCP Gateway + `SMARTCS_KNOWLEDGE_TRANSPORT=mcp` 一键演示

### ⑧ P2：单实例边界 + 测试隔离固化
- 文档明确"单实例运行，不声明多副本支持"（README/architecture）
- tests/README.md 补 per-suite 独立 DB 后缀方案说明（**注意**：仓库 MySQL 用户无 CREATE DATABASE 权限——6b 实测——方案需一次性授权引导，写清步骤即可不执行）

## 2. 硬约束

1. **服务可用性**：改动批量完成后一次重启（重启前 SendMessage 告知我，用户在验收）；容器形态（8001）重启后一并重建镜像复验
2. 全量回归：TS 全绿 + pytest ≥ 641 + tsc 干净（**串行 + 测试库窗口纪律**，机器内存临界——重型测试绝不并发，必要时报告里声明占用窗口）
3. python-impl 白名单：`internal_api/`（如需）+ docs + README；业务目录（agents/mcp/rag/memory/context/auth/tickets）仍零改动；TS 侧 pi-harness 自由
4. 不 commit / 不 push（验收后我统一提交）
5. 诚实口径照旧；**完成以 SendMessage 通知我为准**（勿依赖文件监听——本机内存压力会杀后台监听）；报告写 `pi-harness/PHASE10_REPORT.md`，终行行首 `PHASE10_DONE <状态词>`（正文勿引用标记字样）
