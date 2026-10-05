# 交接指令：Phase 11 — 最终收口（仓库形态 + 运维就绪，GPT 方案已裁决采纳）

> **发件方**：Claude Code（规划与验收方） ｜ **日期**：2026-10-05
> **前置**：Phase 10 验收通过；**pi-harness 已由验收方以 subtree 并入仓库**（commit 18fa087，历史保留）——本轮从"修路径"开始。
> **方案原文**：`docs/SmartCS_Final_Closeout_Plan_Repository_Operational_Readiness.md`（用户批准执行；下述裁决覆盖其冲突处）。

---

## 0. 裁决汇总（与方案的差异，以此为准）

1. **git 手术已完成**（subtree 保留历史）；执行方**不重复导入**，从路径适配开始。
2. Phase 3 README = 核对微调（Phase 10 已重写过），不推倒重来。
3. 退避**不加 schema 列**：从 `(memory_attempts, updated_at)` 在 dispatcher 计算下次可试时间；实现别扭则保持固定间隔并报备。
4. CI：`npm run test:ci` = 纯离线子集（逐文件论证为何离线）+ typecheck；**不许让 CI 假绿**。
5. Phase 12 停 native 8971 是**硬门禁**：等验收方确认用户验收结束（SendMessage 问我）。
6. **不 push**（workflow 文件就位即止，激活由用户决定）；不 commit（本轮改动验收方统一提交）。

## 1. 执行范围（按序）

### ① Monorepo 路径适配（方案的 Phase 2）
- 清理一切 sibling 假设：`compose.yaml`（`../pi-harness` → `./pi-harness`）、`pi-harness/src/config/env.ts`（`.env` 路径）、`tests/helpers/*`（PYTHON_IMPL 解析→仓库根）、scripts、Dockerfile、README/docs 路径引用。
- **运行中的服务不受影响**（它们从旧 sibling 目录起，别动）；sibling 目录本轮**不删除**。
- 验证：monorepo 内 `npm ci` → `npm run typecheck` → 全量 vitest + pytest 全绿（路径修复后**在仓库内的 pi-harness/ 跑**，测试库窗口纪律照旧）。

### ② Harness `/ready`（方案 Phase 4-5、8）
- `/health` 保持轻量 liveness 不动；新增 `/ready`：MySQL `SELECT 1` + Python `/internal/ready`（新增，Python 侧同样轻量：平台库/工具运行时/记忆运行时连通性，**严禁** LLM/RAG/业务工具）+ 仅 `transport=mcp` 时查 MCP Gateway `/health`。
- 503 仅因依赖不可达；**memory backlog 不拖死 readiness**（`pending>0` → `ready:true, degraded:true, warnings[]`）。
- 响应不泄露任何密钥/URL/SQL 栈。compose 的 harness healthcheck 改打 `/ready`。

### ③ Outbox 观测 + 人工恢复（方案 Phase 6-7、9-10）
- `ReceiptStore.memoryOutboxStats()`（聚合查询，走现有索引，全表扫描禁止）+ dispatcher 进程指标（非权威，重启归零）。
- `GET /internal/ops/memory-outbox`（service JWT 保护，只读不 retry）。
- CLI：`npm run outbox:status` / `outbox:retry`（`--receipt-id` / `--failed --limit`）；行为仅 `failed→pending, attempts=0` 交回 dispatcher——**CLI 绝不直接调 enqueue/模型/工具**，恢复路径唯一。
- 退避按裁决 3（无 schema 变更）。

### ④ 两个一致性修正（方案 Phase 11）
- Prompt 身份句改中性（能力细节交给动态工具面段落）。
- 全部文档清理 "exactly-once/恰好一次" 表述 → "At-least-once delivery + idempotent consumer"。

### ⑤ 文档与 CI（方案 Phase 3、13）
- 根 README 第一屏核对微调（Pi 架构先行）；架构三文档路径引用适配 monorepo。
- `.github/workflows/`：Python 侧保持既有；新增 Node job（Node 22 + `npm ci` + typecheck + `test:ci`）。

### ⑥ 本地形态收敛（方案 Phase 12）——**门禁项**
- 用户提供验收结束后：停 native 8971，Docker Compose 成为唯一形态（含从 monorepo 路径重建镜像 + 冒烟）。**执行前 SendMessage 向我确认**。

## 2. 硬约束

1. 方案的"本轮明确不做"清单**全部照办**；不顺手重构（方案的执行约束节全文有效）。
2. 服务重启/收敛**必须显式带 4 个进程级 env**（ROLLOUT/WRITE_MODE/SKILLS/TOOL_TIMEOUT）——Phase 10 踩过的静默降级坑，启动行自检会公告。
3. 仓库内测试跑全量（vitest + pytest 基线 675/2/1→已修）；机器纪律（串行、内存临界、测试库窗口声明）。
4. 不 commit / 不 push / 不删 sibling 目录；完成以 **SendMessage** 通知 + 报告 `pi-harness/PHASE11_REPORT.md`（终行行首 `PHASE11_DONE <状态词>`，正文勿引用标记字样）。
5. 方案 Phase 14 验收清单**逐项给证据**（其中 git 相关三项由验收方负责，你标注 N/A-验收方）。
