# 交接指令：Phase 8 执行（ds-for-act 专用）— 可选演进三件套（最后一个执行阶段）

> **发件方**：Claude Code（规划与验收方） ｜ **日期**：2026-10-04
> **前置**：Phase 0-7 全部验收通过（终态 pytest 636/37、TS 142/142）。用户已确认 Phase 8 三项全做 + 终局后分层提交。
> **必读**：`python-impl/docs/phase8-design.md`（唯一详细设计，含 MCP 化的诚实限界）。

---

## 1. 执行范围

1. **knowledge_search MCP 化**（唯一落地的 MCP 项）：Python `internal_api/mcp_gateway.py`（官方 mcp 包，streamable HTTP，server 级 token 鉴权）+ TS 内建 MCP client 挂载 + `SMARTCS_KNOWLEDGE_TRANSPORT=http|mcp` 开关（默认 http）；HTTP 薄壳保留为对照与回退。
2. **4 个身份绑定工具的 MCP 可行性评估报告**（三方案对比，只报告不落地）。
3. **Skills 渐进披露**：3 个 Skill 经显式 resourceLoader 注册（禁 `~/.pi` 发现），语义红线 = 不碰任何业务判定路径。
4. **Subagent 评估**：混合场景实测对比 + 工具面敏感性分析 → `pi-harness/docs/subagent-evaluation.md`，明确建议，不落地。
5. P8-1～P8-8 全部实现并测试。

## 2. 硬约束

1. **零回归**：默认配置（http 传输、Skills 可关）下行为与 Phase 7 终态完全一致——P8-3/P8-7 是硬门禁。
2. 安全模型不变：无用户身份进 MCP 通道；Skills 不参与任何"能不能"判定（P5/F 抽样回归）。
3. python-impl 白名单新增：`internal_api/mcp_gateway.py` + 对应 tests；其余业务目录照旧只读。
4. 机器/窗口纪律沿用（跨会话 SendMessage 协商窗口；全量基线约 7+8 分钟串行）。
5. pytest ≥ 636；TS 全绿；tsc 干净；不 commit/push；报告 + 终行完成标记照旧。

## 3. 交付物

`PHASE8_REPORT.md`（P8-1～P8-8、双传输 parity 数据、偏差、`STATUS:`、终行完成标记）+ `pi-harness/docs/subagent-evaluation.md`。完成后由验收方做终局验收（全阶段汇总 + 统一复跑 + 分层提交清单），届时另行下发。
