# SmartCS × Pi Harness 迁移·终局验收报告

> **验收方**：Claude Code（python-impl-79 终端） ｜ **执行方**：Claude Code（python-impl-81 终端，ds-for-act）
> **日期**：2026-10-04 ｜ **结论**：**迁移完成，验收通过。**

---

## 1. 最终判定

计划 v2 的全部阶段（Phase 0-7 主体 + 6b/4B/5F-2/5a-d 返修轮 + Phase 8 可选演进）均已执行、独立复验并验收通过。系统达成设计目标：**Pi（Node/TS）= Agent Runtime，Python = Business Runtime** 的双层架构，且原 Python 业务资产（RAG/记忆/幂等执行/审批/数据层）零重写、legacy 链路一行未改、全程可回退。

## 2. 阶段验收总表

| 阶段 | 内容 | 验收 | 关键产出 |
|---|---|---|---|
| 0 | 版本审计 + SDK 断言 A1-A8 + spike | ✅ | 冻结 1.0.1；12 项 API delta |
| 1 | Session/Receipt 基础 | ✅ | 单写者、幂等回执、崩溃恢复、先查后建纪律 |
| 2 | 只读工具 + 内部通道 | ✅ | 三重身份绑定、越权/注入测试 |
| 3 | Context/Memory/Compliance | ✅ | Turn Snapshot、durable outbox、message_end 合规 replacement |
| 4/4B | WRITE shadow + 真实模型验证 | ✅ | 门禁三轮全零、91.7% 一致率 |
| 5(a-d/F/F-2) | 真实 WRITE 全套 | ✅ | 两段式授权、operation_id 先于发送、UNKNOWN/reconcile、**F1-F14 真实故障注入全过** |
| 6/6b | Observability + 测试基建修复 | ✅ | trace 全链路、审计管道、凭据自举 + fail-fast |
| 7 | Cohort 灰度 | ✅ | 确定性分桶、统一入口分发、宁 503 不降级 |
| 8 | 可选演进 | ✅ | knowledge_search MCP 化、Skills 渐进披露、两份评估报告（维持单 Agent；HTTP 薄壳为合法终态） |

**终态数字**（双方独占窗口独立运行，逐项一致）：pytest **641 passed / 37 skipped**（原基线 512 → **+129**，零破坏）；TS **151/151（30 文件）**；`tsc` 干净；HEAD 全程零 commit（留待本次分层提交）。

## 3. 安全成果（迁移的硬性目标，全部有代码级证据）

- 模型决策 ≠ 业务执行：`confirmed` 永非模型参数；授权只依赖 MySQL 权威 + service JWT claims；raw 用户消息从 provenance 账本回读
- WRITE 全链：`operation_id` 先于发送 durable → UNKNOWN 禁盲重试 → reconcile 按账簿收口（不咨询模型）；abort ≠ 业务失败
- F1-F14 十四类真实故障注入（真进程崩溃/socket 销毁/SSE 断连/悬挂 claim）全过 + F4/F5/F9 整合链路一条链跑通
- 真实模型（kimi）三轮门禁：无授权写尝试 = 0、两段纪律违反 = 0（唯一跨轮稳定结论）
- prompt injection 前哨：注入文本进入模型上下文但授权链够不到（"模型完全照做"的最坏情形下仍零写入）

## 4. 交付物清单

- **`pi-harness/`**（新目录，独立建仓提交）：src（agent/session/business/streaming/tracing/server/mcp/skills）、tests（30 文件 151 用例）、scripts、skills（3 技能）、docs（subagent-evaluation、mcp-feasibility）、PHASE0-8 报告
- **`python-impl/`**（白名单内增量）：`internal_api/`（10 模块：auth/tools/context/memory/compliance/operation_status/write_authorization/mcp_gateway/service_jwt/harness_client）、`migrations/`（001-004）、`api/main.py`+`platform_db/sessions.py` 最小 diff、20 个新测试文件
- **`docs/`**：计划 v2、8 份阶段设计、17 份交接指令、3 份过程决策记录
- 灰度开关：`SMARTCS_PI_ROLLOUT_PERCENT`（默认 0 = 全 legacy，上线节奏由用户掌握）

## 5. 遗留项登记（不阻塞验收，按优先级）

1. **部署前置**：生产库需依次执行 migrations 001-004（幂等）；pi-harness 未容器化（compose 未含该服务）；dev 库 `smartcs_checkpoint` 尚无 `harness_version` 列
2. **F9 SQLite 锁竞态 flaky**：`waitFor` 不容忍 `SQLITE_BUSY`（已定性非回归，隔离双跑全绿；低成本可修）
3. **Phase 8 的 4 个 env 开关未入 `.env.example`**（`SMARTCS_KNOWLEDGE_TRANSPORT/MCP_URL/MCP_TOKEN/SKILLS`，语义见 PHASE8_REPORT §8）
4. **dg-piagent skill 升级待办**（用户已批准、执行中被暂停）：回流清单=各阶段实测勘误（bindExtensions 必须 await、`mcp/` 包遮蔽、工具白名单=工具面、Skills 无模型侧入口、durability 守卫、session v3 等）
5. **上游反馈素材**（PHASE8_REPORT §8 五条）：SDK 宿主的 skill 模型可见入口缺失等
6. RAG benchmark 数值复跑（P2-8 加强项）：以结构性证据（零 diff + 测试全绿）判定，数值复跑需专门机器窗口
7. `internal_api/tools.py` 生产侧静默降级语义（platform_database 缺失时 pending 被跳过）：测试侧已 fail-fast，生产侧改显式报错待定

## 6. 过程资产（方法论沉淀）

双会话分工（规划/验收 vs 执行）+ 返修循环 + 诚实口径（跑了且失败=failed / 未跑完=blocked）+ 跨会话协商 + 测试库独占窗口纪律 + MSYS 陷阱对策 + 报告状态载体协议——全部在实战中验证并记录于项目记忆与各报告偏差节。

---

**验收签字**：Claude Code（验收方）——全部阶段独立复验，数字逐项一致，无未披露偏差。
