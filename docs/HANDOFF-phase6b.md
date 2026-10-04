# 交接指令：Phase 6b — 测试基础设施修复轮（小轮，不动业务代码）

> **发件方**：Claude Code（规划与验收方） ｜ **日期**：2026-10-04
> **前置**：Phase 6 验收已通过（136/136 + 585/37 均独立复现）。本轮修的是**验收期暴露的两个测试基础设施缺陷**——你的报告数字从头到尾是诚实的，失败全在基建。

---

## 1. 修复项

1. **fixture 凭据自举**：`tests/helpers/phase1.ts`（及 f-matrix/matrix-harness 同源处）spawn Python 服务前，从 `python-impl/.env` 读取 `MYSQL_*`（复用 `src/config/env.ts` 的既有加载逻辑），**显式并入 spawn env**（进程 env 优先、.env 回退）。修掉"干净 shell 下 `platform_database` 静默缺失 → pending_action 静默跳过"这条链——顺带给该静默路径加一条防御：`platform_database` 缺失且工具是 refund_evaluate 时，服务启动即 fail-fast（测试环境配置错误应该响，不该哑）。
2. **测试库按 runner 分离**：确认 TS fixture（`SMARTCS_TEST_DATABASE`，已有）与 Python helpers 的库名变量打通同一约定；本轮起**验收方用独立库名**（我方后续跑 `SMARTCS_TEST_DATABASE=smartcs_claude_verify`），你保持默认。两者互不踩。
3. **seedAccount 幂等**：`INSERT ... ON DUPLICATE KEY UPDATE`（或先删后插），杜绝失败中途残留用户引发的级联假失败（实测 `Duplicate entry 'f8-owner'`）。
4. **占用窗口纪律落文档**：在 `pi-harness/tests/README.md`（新建，简短）写明：动共享库的套件运行前在终端声明窗口；两个 runner 默认库名已分离，共享仅剩显式选择。

## 2. 硬约束

- 只动 `pi-harness/`（helpers + 新 README）与 `python-impl` 的 `tests/internal_api_helpers.py`（若需打通库名变量）；**业务代码零改动**；pytest ≥ 585 与 TS 全绿是验收线（本轮自己的用例可加可不加）。
- 干净 shell 复现验证：**unset MYSQL_PASSWORD 环境后**全量 vitest 仍须全绿（这是修复项 1 的验收判据）。
- 机器纪律 + 测试库窗口纪律（本轮你自己也要声明占用）沿用；不 commit/push。

## 3. 交付物

`PHASE6_REPORT.md` 追加"6b 修复轮"章节：修复对照、**干净 shell 复现证据**（unset 凭据后全量绿）、偏差、`STATUS:`、终行完成标记。
