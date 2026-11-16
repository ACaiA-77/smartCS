# 重构落地记录（2026-11-17）

约束：**不减功能**；每个改动走独立 PR 与完整 CI；不修改冻结的阶段计划或历史报告
（与 `docs/refactor-baseline.md` 的规则一致）。

## 已合入

| PR | 范围 | 结果 |
| --- | --- | --- |
| #5 | CI 补齐：真实 MySQL/Redis、跨服务 e2e、完整 Harness 套件、JUnit 覆盖门禁 | Python 与 Harness 任务全量通过；门禁会拦住关键套件缺席、非批准跳过和空报告 |
| #5 | 测试**进程树**终止（Linux） | `tsx` 会再 fork 一层，旧逻辑只杀直接子进程；改为进程组 + 端口释放等待 + 每轮启动令牌。详见 `docs/refactor-baseline.md` |
| #6 | `mcp/` → `tool_runtime/` | 与官方 `mcp` SDK 同名遮蔽问题消除；删除未使用的 `server/registry/types/transport/tools`；68 文件、净 −623 行 |
| #8 | `policies/` 中性规则层 | 合规规则表/脱敏/判定与两条同意规则单一来源；`internal_api` 不再经 `agents` 间接依赖 langchain |
| #9 | `db/mysql.py` | 连接配置、环境读取、取消后等待数据库线程统一；事务、会话锁与错误映射仍由各 store 保留 |
| #10 | `memory/user_memory_search.py` | 词法检索（分词/查询词上限/重叠分）从 1.8k 行服务模块移出，调用点与排序不变 |
| #11 | `context/projection.py` | 纯投影规则（状态白名单、消息归一化、checkpoint 合并）与有副作用的编排分离；`manager.py` 以原私有名重导出 |

每个 PR 都在 CI 上运行完整 Python 套件（含真实 MySQL/Redis）与完整 Harness
套件；本地另用可丢弃的 MySQL/Redis 容器做同构复核，两个运行器不并发共享测试库。

## 刻意不做

这三点都在路线上出现过，但改动只会增加间接层或改变既有语义：

1. **`api/main.py` 路由拆分。** 该文件是组合根：`lifespan` 设置 `app.state` 句柄，
   端点直接读取模块级句柄。拆成 `APIRouter` 需要把句柄改成依赖注入或
   `request.app.state`，再为每个路由组配一个工厂——多一层间接，概念数量不变。
   当前约 1000 行按"认证 / 会话 / 聊天 / 检查点与历史 / 工具与审批 / 指标"分段。
2. **编排器双路径合并。** `checkpoint_store=None` 的兼容路径是 README 明确保留的
   离线编排行为；checkpoint 路径的阶段保存与断点恢复由 F 矩阵固定。合并会改变
   恢复语义，属于功能变更而非重构。
3. **`agents/intent_router.py` 的确认短语。** 它是精确匹配集合，与
   `policies.consent.is_refund_confirmation` 的子串规则不同；合并会改变旧路径行为。
   选择在 `policies/consent.py` 文档中注明两者的区别。

## 如何复核

```bash
python -m pytest -q -rs --junitxml=test-results/python.xml
python -m scripts.check_ci_test_results test-results/python.xml
npm --prefix pi-harness test
python -m scripts.check_repository_readiness
```

真实存储测试需要 `SMARTCS_CHECKPOINT_MYSQL_TEST=1`、`SMARTCS_AUTH_MYSQL_TEST=1`、
`SMARTCS_CONTEXT_REDIS_TEST=1` 与可达的 MySQL/Redis；规则见
`pi-harness/tests/README.md`。
