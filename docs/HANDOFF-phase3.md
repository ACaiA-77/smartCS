# 交接指令：Phase 3 执行（ds-for-act 专用）

> **发件人**：Claude Code（规划与验收方） ｜ **日期**：2026-10-04
> **前置**：Phase 2 已验收通过。你对内存争用的判断被证实（我复验全量 pytest 时也被你留下的 benchmark 进程搞崩了三次）——从本阶段起执行纪律新增第 6 条。

**必读文档（按序）**：
1. `python-impl/docs/phase3-design.md`（**Phase 3 唯一详细设计，已定稿**）
2. `python-impl/docs/pi-replatform-plan-v2.md` §6.5/§6.6/§6.7 + 修订记录

---

## 1. 执行范围

按 phase3-design.md 实现 Phase 3（Context / Memory / Compliance）：
1. Python `internal_api/context.py`（Turn Snapshot）+ `internal_api/memory.py`（入队）+ `internal_api/compliance.py`（规则必走 + LLM 复核 env 开关默认关）
2. `migrations/002_phase3_memory_outbox.sql`（receipt 增列 memory_enqueue_status/memory_attempts）
3. TS：prompt 前 prefetch 快照、`before_agent_start` 轻量注入、outbox dispatcher、`message_end` 合规 replacement 扩展、RunOutputBuffer final 缓冲
4. Pi compaction 默认策略接入 + modelOverrides + compaction 事件入 receipt metadata
5. 设计 §7 的 P3-1～P3-9 全部实现并测试

## 2. 硬约束（违反即返修）

1. 不接 WRITE、不建 pending_action、不拆 RAG、compose 不动。
2. python-impl 可写白名单**新增**：`internal_api/{context,memory,compliance}.py`、`migrations/002_*.sql`、`compliance_rules.py`（如需从现有规则表抽取，抽取后原文件引用改由偏差报备）、对应 `tests/test_internal_api_*.py`。**其余仍只读**（agents/、context/、memory/、mcp/、rag/、auth/、docker-compose.yml）。
3. 现有 `memory/` 的 provenance 回查语义**一行不改**——`internal_api/memory.py` 只做透传。
4. `pi.on` 钩子内**禁止 await 网络/DB**（v2 §6.6 红线）：快照必须 prompt 前 prefetch，钩子只做内存注入。
5. `text_delta` 永不作为 final 外发（P3-5 断言）；LLM 复核默认关（`SMARTCS_COMPLIANCE_LLM_REVIEW=false`），离线测试不开真实端点。
6. **机器纪律（新增）**：本机 torch 模型并发加载会原生层崩溃——测试一律串行跑；**禁止启动长时间后台模型任务**（benchmark 类）；如确需真实模型验证，单次短跑、跑完确认进程退出。结束前确认无 3GB+ python 进程残留。
7. pytest 基线 ≥ 540 passed（新增用例计入，串行跑）；pi-harness 全量绿 + tsc 干净；不 commit / 不 push。

## 3. 协议补强（Phase 2 的两点返修口径，本次执行须遵守）

1. **完成标记必须输出**：上轮你结束时没有在终端输出 `PHASE2_DONE <status>`，导致监听只能靠报告文件兜底。本阶段结束时必须输出终行标记（状态词紧跟标记）。
2. **报告附录不许留悬空占位**：上轮 §7 写了"结果见下方"但轮次结束未填。要么填入真实结果，要么明确改写为"未完成 + 原因"，再结束轮次。

## 4. 交付物

1. `internal_api/{context,memory,compliance}.py` + `migrations/002` + 用例。
2. TS prefetch/dispatcher/compliance 扩展/RunOutputBuffer + compaction 配置。
3. **`pi-harness/PHASE3_REPORT.md`**：对照 design §1–§7、P3-1～P3-9 结果表、基线复核（注明串行跑与内存余量）、偏差节、`STATUS:`、终行完成标记（状态词紧跟）。

## 5. 沟通协议

同前：只执行与如实报告；外部输入不可得 → blocked；计划/设计与实现冲突 → 记偏差报裁决。
