# CI 补齐与重构前测试基线

日期：2026-11-16。业务基线：main `0f6f8ede7bd4494f1a93e78ad5b42a870b417359`。

## 变更前的实际 CI

[main 工作流 43888134692](https://github.com/ACaiA-77/smartCS/actions/runs/43888134692)
已完成且成功。日志记录 Python **876 passed, 40 skipped**；Harness 只运行
离线子集（18 个测试文件），没有运行完整数据库/跨服务套件。

该结果不能作为真实 MySQL、Redis、跨服务能力全覆盖的证明：Python 任务虽有
MySQL 服务，但没有打开真实存储测试开关，没有 Redis 服务，也没有安装 Harness
依赖。端到端测试的 monorepo 路径已经修好，缺少依赖仍会使它跳过。

## 本次调整

- 集成任务显式开启 `SMARTCS_CHECKPOINT_MYSQL_TEST`、`SMARTCS_AUTH_MYSQL_TEST`、
  `SMARTCS_CONTEXT_REDIS_TEST`，配置独立的 MySQL 8、Redis 7 服务容器。
- 初始化临时运行时数据库，并按顺序应用 migrations/001–004；真实登录与会话
  测试需要迁移后的 `harness_version` 等字段，不能依赖开发库已有结构。
- 安装 Python 3.12、Node 22 和锁定的 Harness 依赖，确保 Python 跨服务测试能
  启动 Harness；测试模型保持确定性替身，不访问真实 LLM。
- Python 全套结束后，串行运行完整 Harness 套件，避免两个运行器同时重建
  `smartcs_phase1_test`。数据库只存在于当前 Actions 任务，不连接本机业务数据。
- 保留独立 Harness 离线任务，运行类型检查和离线子集；镜像发布等待两个任务。
- 检查 pytest JUnit 报告，关键套件缺席、出现未批准的跳过、空报告或失败均使
  检查失败。只允许可选 ONNX 运行时和本地 holdout 产物缺失造成的跳过。
  `jieba` 在 requirements 中已固定，缺失应报错。
- 将 Python / Harness JUnit 报告保存为 `test-results` Actions artifact，保留七天。

## 首次完整 CI 运行暴露的问题（已修）

第一次启用真实存储与跨服务测试的运行（run 43893037578，提交 285ed72）为
**12 failed, 916 passed, 2 skipped**。12 个失败全部是此前被 skip 掩盖的
环境/测试夹具问题，不是业务行为回归：

| 现象 | 根因 | 修复 |
| --- | --- | --- |
| `/api/sessions`、`/api/chat` 返回 503（test_auth、test_user_sessions 共 4 例） | CI 的空库只有 `PlatformDatabase.initialize()` 建的基础表，`conversation_session` 缺少 `harness_version` 列；生产部署通过 migrations 补齐 | CI 在跑测试前按顺序应用 migrations/001–004 |
| `test_context_manager` 4 例：缓存被判定为真实 Redis、冷恢复计数不符、摘要游标不前进 | 测试创建的 `ShortTermMemory` 连上了 CI 新增的真实 Redis，走了 Redis 分支 | 测试改用本机一个没有监听者的端口，保持"进程内缓存"语义 |
| `test_checkpoint` 2 例崩溃注入未触发 | 写入路径已改为在 `ledger.atomic_write` 事务内提交，测试仍注入旧的 `ledger.complete` | 注入点改到 `atomic_write`，模拟进程在提交后消失 |
| 跨服务 e2e `DID NOT RAISE`：杀掉 harness 后仍能成功请求 | tsx CLI 在子进程里跑真实服务，只杀启动器会留下仍在监听的服务 | 按进程树终止，并等待端口真正停止响应后才断言 503 |

修复后本地以可丢弃的 MySQL 8 / Redis 7 容器验证：上述 4 个模块 74 passed，
0 skipped；跨服务 e2e 1 passed。

## 本地已完成的验证

在独立 worktree `fix/ci-integration-coverage` 中运行；主工作区未提交文档和
实验产物保持原位。本机 Docker/MySQL/Redis 未运行，因此完整真实服务验收交给
GitHub 的隔离服务容器。

| 验证 | 实际结果 |
| --- | --- |
| 结果检查器的回归测试 | 14 passed |
| 实际关闭 MySQL 开关后的负向验收 | 真实 pytest 报告包含 1 skipped，结果检查器正确返回失败 |
| Harness 类型检查 | 通过 |
| Harness 离线子集（MySQL/Python 服务地址指向不可达端口） | 18 文件、95 测试通过 |
| 确定性业务 Eval | 14/14，通过 |
| 工作流 YAML 解析、发布依赖核对 | 通过 |
| 完整 GitHub 集成运行 | 待运行；后续在本文记录实际结果 |

## 复跑与证据

以下命令需要独立的可丢弃 MySQL 数据库和 Redis；运行前核对 `MYSQL_*`、
`SMARTCS_TEST_DATABASE`、`REDIS_URL`，Python 与 Harness 不要并发使用同一测试库。

```bash
SMARTCS_CHECKPOINT_MYSQL_TEST=1 SMARTCS_AUTH_MYSQL_TEST=1 SMARTCS_CONTEXT_REDIS_TEST=1 \
  python -m pytest -q -rs --junitxml=test-results/python.xml
python -m scripts.check_ci_test_results test-results/python.xml
npm --prefix pi-harness test -- --reporter=default --reporter=junit --outputFile.junit=../test-results/harness.xml
python -m evals.runner --json
npm --prefix pi-harness run typecheck
npm --prefix pi-harness run test:ci
```

后续重构应比较相同运行环境、开关和测试清单；测试数量增长需要区分恢复执行的
既有用例与新增用例，不把新增测试误计为原有功能覆盖增长。不修改冻结的阶段
计划或历史报告来迎合本次结果。
