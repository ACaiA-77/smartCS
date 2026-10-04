# Phase 4 详细设计：WRITE Shadow Mode（定稿 v1）

> **状态**：定稿（Phase 3 验收后下发） ｜ **日期**：2026-10-04
> **依据**：`pi-replatform-plan-v2.md` §10 Phase 4 + §6.4（三种授权模式）。
> **范围红线**：**零真实 WRITE 执行**（这是 shadow 的定义）；python-impl 预期零改动；不建 pending_action；compose 不动。
> **目标**：验证 Main Agent 在面对写操作时的决策质量，与 legacy 对拍，达标才允许 Phase 5 开真实写。

---

## 1. Write 模式开关

```text
SMARTCS_WRITE_MODE = off | shadow | live
  off（默认，Phase 0-3 行为）: 模型工具面不含任何写工具
  shadow（本阶段）          : 模型可见 refund_confirm / ticket_create，调用被拦截记录，零执行
  live（Phase 5，另行交接）  : 真实执行（含 pending_action、operation_id、reconcile 全套）
```

shadow 与 live 的工具 **schema 必须完全一致**（同一份定义，仅执行分支不同）——否则 shadow 的对拍结论对 live 无预测力。

## 2. Shadow 工具语义

### 2.1 工具面（Phase 4 起，shadow 模式注册）

- `refund_evaluate`（已有 READ）→ shadow 模式下返回中**附带** `pending_action_id` 字段（格式与 Phase 5 计划一致，值为确定性占位如 `pending-shadow-<hash>`）——模拟两段式的第一段。**不写任何库**。
- `refund_confirm(pending_action_id)` —— shadow：拦截，返回确定性结果 `[SHADOW] 退款确认已记录为计划，未执行`。
- `ticket_create(...)` —— schema 按 Phase 5 目标面（业务参数，**无 confirmed 参数**）—— shadow：拦截，返回 `[SHADOW] 工单创建已记录为计划，未执行`。

### 2.2 拦截实现

工具 execute 内按 `SMARTCS_WRITE_MODE` 分支：`shadow` → 写计划记录 + 返回 canned 结果（**不发任何 internal HTTP**）；`off` → 该工具不注册；`live` → 本阶段不实现（留 TODO + 显式 throw，防误开）。

## 3. 计划记录（shadow_write_plan）

```text
MySQL（smartcs_phase1_test 库，随迁移建表，migrations 由 pi-harness 侧 SQL 或 TS 建表均可——不进 python-impl/migrations）
shadow_write_plan
  id · session_id · client_request_id · tool_name · arguments(JSON)
  turn_index · user_message_excerpt(≤200 chars) · created_at
  KEY(session_id, client_request_id)
```

每次 shadow 拦截插入一行。这是对拍与审计的唯一数据源。

## 4. 对拍设计（决策质量门禁）

### 4.1 场景集

复用现有 `evals/`（Python）的**业务场景断言**选出的对拍集（退款确认、工单创建、闲聊、知识问答、越权尝试，≥ 12 条），在 `pi-harness/tests/fixtures/` 落成 JSON 场景文件（多轮：含"评估→确认"两轮序列）。

### 4.2 双侧运行

- **pi 侧**：Faux provider 脚本化模型（按场景预置工具调用序列，模拟模型决策），跑完整管线（含 receipt/registry/compliance），产出 shadow_write_plan 记录。
- **legacy 侧**：**必须复用现有 `evals/` 基础设施**（DeterministicEvalLLM / ObservedExecutor 那套）跑同一场景集，产出决策记录（是否 refund/ticket、参数、是否等待确认）。**禁止重新实现 legacy 决策逻辑**——通过子进程调用现有 evals runner 或其可复用组件；若现有 runner 无法以库形式复用，允许写薄封装脚本放 `pi-harness/`（Python 文件放 pi-harness 下，不进 python-impl）。

### 4.3 对比指标（Phase 5 门禁阈值）

| 指标 | 阈值 |
|---|---|
| 工具选择一致率（refund/ticket/无写，逐场景对比 legacy） | ≥ 90% |
| **越权/无授权写尝试数**（用户未表达写意图时模型调用写工具） | **= 0（硬门禁）** |
| refund 两段纪律违反（未经用户确认轮就 confirm） | = 0（硬门禁） |
| 写参数关键字段一致率（order_id 等，与 legacy 对比） | ≥ 90% |
| 场景覆盖 | 全部 ≥12 场景双侧跑通 |

不一致场景逐条列 diff（哪一侧、什么差异、疑似原因）——不一致本身不阻塞，**硬门禁与阈值**才阻塞。

## 5. 白名单

**python-impl 预期零改动**（若实现中发现必须改 python-impl 才能完成对拍，停下记偏差报裁决，不得自行改）。全部新代码落 `pi-harness/`。

## 6. 验收用例（Phase 4 门禁）

| # | 场景 | 必须保证 |
|---|---|---|
| P4-1 | SMARTCS_WRITE_MODE=off | 工具面不含写工具（回归 Phase 3 行为） |
| P4-2 | shadow 模式正常链路 | 写工具调用 → shadow_write_plan 恰好一行 + canned 结果给模型 + **无任何 internal HTTP 写调用**（断言网络层） |
| P4-3 | 越权场景（用户无写意图） | 模型（脚本化）不调用写工具；即便调用了（恶意脚本模拟）→ 仍只记计划零执行 |
| P4-4 | refund 两段纪律 | "评估→未确认"轮：无 confirm 计划；"评估→确认"轮：恰一条 confirm 计划且 pending_action_id 与 evaluate 返回一致 |
| P4-5 | ticket same-turn | 用户明确建单 → 恰一条 ticket_create 计划，参数与场景一致 |
| P4-6 | 对拍报告 | ≥12 场景双侧 + 指标表 + 硬门禁全过 + 不一致 diff 清单 |
| P4-7 | 基线 | pytest 559/37 不变（python-impl 零改动的直接推论，仍要实测）；TS 全绿 + tsc 干净 |

## 7. 交付物

`pi-harness/`：write-mode 开关 + shadow 工具 + shadow_write_plan 存储 + 场景 fixtures + 双侧对拍 runner + 报告；**`pi-harness/PHASE4_REPORT.md`**（实现对照、P4-1～P4-7、对拍指标表与 diff、偏差、`STATUS:`、终行完成标记）。
