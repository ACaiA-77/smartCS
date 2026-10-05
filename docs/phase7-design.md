# Phase 7 详细设计：Cohort 灰度（定稿 v1）

> **状态**：定稿（Phase 6 验收通过；用户已指示 6b 后直接进入） ｜ **日期**：2026-10-04
> **依据**：`pi-replatform-plan-v2.md` §10 Phase 7 + §13 灰度原则。
> **核心原则**：**harness_version 在会话创建时一次性固定，终身不变；绝不按 intent 切换**。灰度单位 = 新建会话的账号分桶。

---

## 1. 灰度开关

```text
SMARTCS_PI_ROLLOUT_PERCENT（int 0-100，默认 0）
分桶函数：bucket(account_id) = (hash(account_id) % 100) < PERCENT → 新会话 harness_version = pi，否则 legacy
```

- **确定性**：同账号恒定同桶（重试/多会话不漂移）；hash 用稳定算法（如 SHA-256 前缀取模），禁止用 Python hash()（进程间随机化）。
- 会话创建点：`POST /api/sessions` 显式创建 + chat 隐式创建，两处都必须走同一分桶函数。
- 写入点：`conversation_session.harness_version` 建行时写死（`platform_db/sessions.py` 的 create 路径加参数，最小 diff——本阶段**授权**该文件写入）。

## 2. 统一入口 chat 分发（单入口 UX，工作台零改动）

```text
POST /api/chat（Python，公网，用户 JWT cookie）
  → 查 session.harness_version
  → legacy：走现有 ChatOrchestrator（一行不改）
  → pi：转发 TS pi-harness /api/chat（internal_api/harness_client 扩展：携带用户 JWT 原文 + 服务签名头）
        → TS 完整管线（Phase 1-6 全套）→ 返回
        → Python 把 TS 响应适配为现有 ChatResponse 形状（response/session_id/intent/compliance_passed/client_request_id）
        → 转发失败 → 503（pi harness unavailable，与 history 分发同一模式）
```

- **pi 会话绝不落入 legacy orchestrator**（拿到 pi 会话却无法转发时宁可 503，也不错误降级到旧链路——降级会造成同一会话双 transcript 体系）。
- history/delete 分发已有（Phase 1），chat 是最后一块，三者口径一致。

## 3. 范围边界

- **不包含** compose 容器化（留给终局交付轮）；不包含 SSE 直连 TS（工作台保持 JSON，Phase 5 已定）。
- 灰度观测：Python 侧在响应/日志中带 `harness_version` 标记（工作台可见当前会话由哪个 harness 服务）。

## 4. 白名单变更（相对 6b）

python-impl 可写**新增**：`platform_db/sessions.py`（**仅** create 路径加 harness_version 参数，最小 diff）、`internal_api/harness_client.py`（chat 转发）、`api/main.py`（session 创建分桶 + chat 分发）、`.env.example`。TS 侧如需适配（响应形状断言）在 pi-harness 自由改动。

## 5. 验收用例（P7-1～P7-7）

| # | 场景 | 必须保证 |
|---|---|---|
| P7-1 | PERCENT=0 | 新会话全部 legacy；legacy 链路端到端正常（回归） |
| P7-2 | PERCENT=100 | 新会话全部 pi；经**统一入口** /api/chat 转发 TS 全链路通（receipt/intent/回执幂等） |
| P7-3 | 分桶确定性 | 同账号反复建会话 → harness_version 恒定；跨进程重启仍恒定 |
| P7-4 | 旧会话不变 | 已存在的 legacy 会话在 PERCENT=100 下仍走 legacy；harness_version 不被改写 |
| P7-5 | 混合灰度 | PERCENT≈50 时两类账号并存，各自链路正确互不串扰 |
| P7-6 | pi 不可达 | 转发失败 → 503，**不降级**到 legacy；恢复后同请求可重试 |
| P7-7 | 基线 | pytest ≥ 585（6b 后值）；TS 全绿；tsc 干净；`agents/` 等其余目录仍零改动 |

## 6. 交付物

灰度开关 + 分桶 + chat 分发 + 用例；**`pi-harness/PHASE7_REPORT.md`**（对照表、P7-1～P7-7、分桶证据（跨重启恒定）、偏差、`STATUS:`、终行完成标记）。
