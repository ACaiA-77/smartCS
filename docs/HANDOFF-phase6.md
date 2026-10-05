# 交接指令：Phase 6 执行（ds-for-act 专用）— Observability

> **发件人**：Claude Code（规划与验收方） ｜ **日期**：2026-10-04
> **前置**：**Phase 5 整体验收通过**（F1-F14 真实注入全过、整合链路跑通、两处真实缺陷修复——你 5F-2 轮的 D1/D2 发现价值极高：5d 的"live 接线"此前实际从未装配。终态 TS 130/130、pytest 576/37 均已独立复现）。
> **必读**：`python-impl/docs/phase6-design.md`（唯一详细设计）+ `pi-replatform-plan-v2.md` §6.9/§10 Phase 6。

---

## 1. 执行范围

按 phase6-design.md 实现：
1. 六 ID 族 + `traceparent` 全链路传播（TS → 每次 internal HTTP → Python span）
2. TS OTel span 树（turn/model/tool/compliance），内存/OTLP 双 exporter，钩子零阻塞
3. 审计管道（有界队列 → 批量 `/internal/audit` → `migrations/004` 落库，幂等去重，best-effort 语义声明）
4. P6-1～P6-6 全部实现并测试

## 2. 硬约束（违反即返修）

1. **观测管道故障不得影响主链路**：pi.on 内禁止 await exporter/网络；审计队列有界（溢出丢弃+计数）；P6-3/P6-4 是硬门禁。
2. **观测关闭态零回归**（P6-5）：无 OTEL 配置时行为与 Phase 5 终态完全一致。
3. 不改业务语义；python-impl 白名单仅新增 `internal_api/audit.py`、`migrations/004`、对应 tests；**tracing/ 现有代码只读**（自动插桩受益即可，覆盖度如实报告）。
4. 新增 npm 依赖须锁定版本进 lockfile（OTel 相关）。
5. 沿用机器纪律、诚实 blocked/failed 口径、报告续写、终行完成标记、不 commit/push。
6. pytest ≥ 576；TS 全绿（Phase 5 的 130 项零回归）+ tsc 干净。

## 3. 交付物

设计 §7 全列 + **`pi-harness/PHASE6_REPORT.md`**（对照表、P6-1～P6-6、span 树实测摘录、审计语义声明、偏差、`STATUS:`、终行完成标记）。
