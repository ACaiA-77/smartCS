# Phase 8 详细设计：可选演进三件套（定稿 v1）

> **状态**：定稿（用户选择：三项全做；交付方式=终局验收后分层提交） ｜ **日期**：2026-10-04
> **依据**：`pi-replatform-plan-v2.md` §10 Phase 8 + 复核稿 §13（Skills 不作 authority）+ §4.3 拓扑 C（MCP 化二期演进）。
> **红线**：全部为**增量演进**——Phase 0-7 已验收的行为零回归；安全模型（身份绑定/授权/幂等）不因任何一项 weakened。

---

## 8.1 READ 工具 MCP 化（诚实限界：一项落地 + 一项评估）

**协议难题先说清**：MCP 连接是长驻的、按 server 鉴权（1.0.1 支持 OAuth/header），**不携带 per-turn 用户身份**。而 4 个 READ 工具（order/ticket/refund_evaluate/risk_check）的身份绑定恰恰依赖 per-turn envelope——让身份穿透 MCP 有三条路（TS 侧注入 wrapper / 每用户连接 / 参数携带 token），各有实质缺陷。**因此：**

- **落地项：`knowledge_search` MCP 化**——共享语料检索，无 per-user 数据分区（现实现中它也不在 `customer_tool_arguments` 强绑集合内），身份语义上可安全 MCP 化：
  - Python 侧新增 `internal_api/mcp_gateway.py`（用 requirements 已有的官方 `mcp` 包，streamable HTTP 模式）：暴露 `knowledge_search(query, top_k)`，内部走同一 HybridRetriever；server 级鉴权（静态 header token，`SMARTCS_MCP_TOKEN`，非用户身份）。
  - TS 侧：pi 内建 MCP client 挂载（`pi.registerMcpServer` 或等价配置，1.0.1 能力，A 断言：连接/工具发现/调用可用）；**HTTP 薄壳路径保留**（对照与回退），由 env 开关选择（`SMARTCS_KNOWLEDGE_TRANSPORT=http|mcp`，默认 http——灰度原则）。
  - **审计不缺位**：TS 扩展的 `pi.on("tool_call"/"tool_result")` 与传输方式无关（事件来自 Pi transcript），MCP 路径的调用照常落审计。
- **评估项：其余 4 工具的 MCP 可行性报告**——三方案（TS wrapper 注入 / 每用户连接 / 参数 token）逐一给出安全性/复杂度/运维代价结论，**只出报告不落地**；结论倾向留 HTTP 薄壳即为合法终态。

验收（P8-1～P8-4）：
| # | 场景 | 必须保证 |
|---|---|---|
| P8-1 | mcp 传输下 knowledge_search 端到端 | 与 http 路径**结果一致**（同 query 同证据，双传输 parity 对照） |
| P8-2 | 身份/安全不变 | 无用户身份进入 MCP 通道；错误 token 拒连；工具面不增长 |
| P8-3 | 回退开关 | 默认 http，行为与 Phase 7 完全一致（零回归断言） |
| P8-4 | 可行性报告 | 三方案结论 + 推荐，可独立复核 |

## 8.2 Skills 渐进披露（知识注入，永不作 authority）

- 经**显式 resourceLoader** 注册 3 个 Skills（服务器白名单来源，**不启用** `~/.pi` 发现）：
  1. `refund-policy`（退款政策说明/话术边界——内容从现有 `knowledge_base/` 政策文档摘编）
  2. `cs-style`（客服沟通风格/工单标准话术）
  3. `tool-guide`（knowledge_search 使用建议 + 工具选择说明）
- **语义红线**（复核稿 §13）：Skills 只影响"怎么说"，不参与"能不能"——pending_action/授权/幂等判定路径零接触（回归断言：P5/F 矩阵抽样用例不因 Skills 存在而变化）。
- 验收（P8-5～P8-7）：渐进披露两级（描述级进 system prompt、正文按需）；compaction 后 Skill 描述仍可再披露（不依赖 transcript）；关闭 Skills 开关零回归。

## 8.3 Subagent 评估（只出报告，不落地）

- 实测设计：同一批混合场景（RAG 密集型 + 交易密集型，脚本化 Faux 模型），对比"单 Main Agent"（现状）与"模拟分上下文"（把两类场景的 prompt+工具集分开测算 token 与干扰度）两组数据；工具面扩张敏感性分析（当前 7 个 → 模拟 15/30 个时的提示词占用）。
- 产出：`pi-harness/docs/subagent-evaluation.md`——结论须回答计划 §22 的触发条件（RAG prompt 与交易 prompt 是否"严重冲突"、上下文隔离收益是否大于子会话成本），给出"维持单 Agent / 引入 Subagent"的明确建议。
- 验收（P8-8）：报告含实测数据表、建议明确、无落地代码。

## 9. 白名单与基线

- python-impl 新增：`internal_api/mcp_gateway.py`、对应 tests；pi-harness 自由改动。
- 基线：pytest ≥ 636、TS 全绿、tsc 干净；机器/窗口纪律沿用（跨会话 SendMessage 协商，动 `smartcs_phase1_test` 前声明窗口）。
- 交付：`pi-harness/PHASE8_REPORT.md`（对照、P8-1～P8-8、偏差、`STATUS:`、终行完成标记）+ `pi-harness/docs/subagent-evaluation.md`。
