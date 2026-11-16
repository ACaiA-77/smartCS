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
- 安装 Python 3.12、Node 22 和锁定的 Harness 依赖，确保 Python 跨服务测试能
  启动 Harness；测试模型保持确定性替身，不访问真实 LLM。
- Python 全套结束后，串行运行完整 Harness 套件，避免两个运行器同时重建
  `smartcs_phase1_test`。数据库只存在于当前 Actions 任务，不连接本机业务数据。
- 保留独立 Harness 离线任务，运行类型检查和离线子集；镜像发布等待两个任务。
- 检查 pytest JUnit 报告，关键套件缺席、出现未批准的跳过、空报告或失败均使
  检查失败。只允许可选 ONNX 运行时和本地 holdout 产物缺失造成的跳过。
  `jieba` 在 requirements 中已固定，缺失应报错。
- 将 Python / Harness JUnit 报告保存为 `test-results` Actions artifact，保留七天。

## 本地已完成的验证

在独立 worktree `fix/ci-integration-coverage` 中运行；主工作区未提交文档和
实验产物保持原位。本机 Docker/MySQL/Redis 未运行，因此完整真实服务验收交给
GitHub 的隔离服务容器。

| 验证 | 实际结果 |
| --- | --- |
| 结果检查器的回归测试 | 14 passed |
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
