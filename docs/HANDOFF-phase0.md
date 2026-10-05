# 交接指令：Phase 0 执行（ds-for-act 专用）

> **发件人**：Claude Code（规划与验收方）
> **收件人**：ds-for-act（执行方，本终端）
> **日期**：2026-10-03
> **权威文档**：`python-impl/docs/pi-replatform-plan-v2.md`（下称"计划 v2"）。本文档只做交接范围界定，与计划 v2 冲突时以计划 v2 为准。

---

## 1. 你的角色

你是 SmartCS × Pi Harness 迁移的**执行方**。规划已完成并冻结，你的职责是按计划实现、测试、如实报告。我（验收方）会在你完成后审查产出；失败项会打回返修，直至验收通过。

## 2. 本次执行范围（严格限定）

**只执行：计划 v2 的 §3 版本审计 + §10 Phase 0（Pi Runtime Spike）。**

1. **版本审计（§3）**：`npm view @earendil-works/pi-coding-agent version` 复核 latest；审阅目标版本 CHANGELOG；**冻结确切 patch 版本**并生成 `package-lock.json`；输出与计划 v2 引用 API 的 delta 对照（写入报告）。
2. **Phase 0 Spike（§10 Phase 0）**：在 `D:\Workspace_for_Codex\project005_SmartCS\pi-harness\`（与 python-impl 平级的新目录，按计划 §4.2 结构）实现：
   - Node 版本确认 + TypeScript 项目初始化（锁定 Pi 版本）
   - DeepSeek/OpenAI 兼容 provider（openai-completions + compat 配置，从现有 `.env` 读取 `OPENAI_BASE_URL`/`OPENAI_API_KEY`/`MODEL_NAME`；若无可用 key，改用 Faux Provider 跑 loop 测试，并在报告中标注 provider 验证被阻塞）
   - 客服系统提示词（覆盖 pi 默认 coding 人设）
   - **禁用全部编码内置工具**（read/bash/edit/write 等）
   - **file-backed SessionManager + 显式 session 目录 + 显式 cwd/agentDir**
   - 2 个 **fake 只读工具**（defineTool，硬编码返回，不调 Python）
   - `session.subscribe` 事件生命周期 + JSON final 输出 + SSE status 通道
   - abort
3. **A1–A8 SDK 断言验证（计划 §10 Phase 0 清单，全部要有代码级证据）**：
   - A1 `SessionManager.create(cwd, dir, {id})` 是否接受调用方指定 id；session id→文件路径可否推导
   - A2 `message_end` 扩展能否返回 replacement message
   - A3 事件顺序：extension `message_end` → public listeners → `SessionManager.appendMessage`
   - A4 file-backed append 落盘时机（durability point）
   - A5 同 session id 被两个进程打开的行为
   - A6 `agent_settled` 每 prompt 恰好一次（含 retry/compaction 场景）
   - A7 reopen 同一 session 恢复 active branch
   - A8 `defaultTools`/白名单在 server 模式下确实禁用了内置工具
   - **证据方式**：每个断言一个最小测试（vitest 或等价），断言失败或 API 不存在时**如实报告，不得绕过或臆造**。验证依据优先级：目标版本 `node_modules` 的 `dist/**/*.d.ts` + `examples/sdk` + CHANGELOG（计划 §3 的兜底协议）。

## 3. 硬约束（违反即返修）

1. **不接真实 WRITE**；fake 工具不调用任何 Python 端点；不改 `python-impl/` 下任何现有代码（只读引用）。
2. **不开始 Phase 1 及之后任何阶段**（不建 MySQL 表、不写 internal_api、不动 auth）。
3. **不 commit、不 push**：全部留在工作区由验收方审查。
4. 每个测试必须可重复运行：报告里给出确切运行命令。
5. 测试必须全绿（或如实列出失败项及原因）；**禁止把失败标成通过**。
6. 遇到计划与 SDK 实际行为冲突：以 SDK 实际为准记录到报告"偏差"节，并说明对计划哪些章节有影响，**不要静默改计划**。

## 4. 交付物

1. `pi-harness/`：spike 代码 + 测试（结构按计划 §4.2，允许最小化）。
2. **`pi-harness/PHASE0_REPORT.md`**（我验收的唯一入口），必须包含：
   - 冻结版本号 + lockfile 摘要（lockfile 内容 hash 或文件大小）
   - A1–A8 结论表：pass / fail / blocked + 每项的证据（测试文件路径 + 关键输出摘录）
   - Phase 0 验收清单逐项结果（loop 可跑 / session 可恢复 / tool_call hook 正常 / 无内置编码工具 / SSE 生命周期正常）
   - 与计划 v2 的 API delta 及偏差说明
   - 测试运行命令与最终结果（含失败项）
   - 状态行：`STATUS: completed | blocked | failed`（blocked 时写明缺什么，如 API key）
3. 报告写完后在终端输出一行 `PHASE0_DONE <STATUS>` 供监控捕获。

## 5. 沟通协议

- 你只管执行与如实报告；验收、计划修订、返修指令由我发起。
- 需要外部输入（如 API key）而不可得 → 写 `STATUS: blocked` 并说明，不要伪造验证。
- 完成定义：报告 + 测试 + 代码齐备，`PHASE0_DONE` 已输出。
