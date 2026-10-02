# Context engineering 实施证据（READY_FOR_REVIEW — 修复轮）

Baseline `b8b6a0c` 之上的本地未提交工作区。未 push / deploy；未启动 API 服务；生产 `.env`、凭据、RAG 索引/模型/评测口径未改。测试进程 `EMBEDDING_BACKEND=hash` 仅限确定性测试；生产保持 `EMBEDDING_BACKEND=local`（BGE-M3）。

本轮（独立审查修复轮）变更聚焦两件事：**A. 真实 Redis 集成验证**（不再模拟）与 **B. 用户记忆后台 Worker**（聊天请求尾部不再内联 drain）。

## A0. Redis 容器与连通性证据

- `compose.yaml` 既有服务 `smartcs-redis`（redis:7-alpine，`127.0.0.1:6379:6379`，AOF 开启，healthcheck `redis-cli ping`）。外部网络 `smartcs-net` 已存在，无需新建网络；仅执行 `docker start smartcs-redis`（未启动 smartcs-api）。
- 容器状态：`Up (healthy)`，`restarts=0`；版本 `redis_version=7.4.9, mode=standalone`。
- 容器侧 `redis-cli ping` → `PONG`；宿主侧 `redis-py ping` → `True`。
- 环境注记：本机 Python 将 `localhost` 解析为 IPv6 `::1`，而容器仅发布 `127.0.0.1:6379`，故验收命令统一使用 `REDIS_URL=redis://127.0.0.1:6379/0` 覆盖（未修改生产 `.env`）。
- 全程零 `FLUSHALL/FLUSHDB`：基准只删除自身 UUID 会话的 WorkingSet key（`cache_loss_method: delete benchmark-owned working-set key`，`shared_redis_flushed: false`），结束后 `redis-cli dbsize` 为 0。

## A1. 真实 Redis benchmark（100/50 轮，含硬门禁 `--require-redis`）

`scripts/evaluate_context_engineering.py` 新增 `--require-redis`：任一构建未走真实 Redis、warm 命中不足、或冷恢复未回填即整体失败。

`artifacts/context_engineering/real_mysql_100_turns_final_review_real_redis_20261118/metrics.json`：

| 指标 | 值 |
|---|---|
| working_set_cache_backend | `["redis"]`（全部 100 轮） |
| working_set_cache_is_real_redis | `true`（逐轮校验） |
| cache hits / misses | 98 / 2（第 1 轮冷填充 + 第 50 轮强制删 key） |
| 第 50 轮冷恢复 | `storage_read_path=cold_event_store_restore`，34.1ms，backend=redis |
| 第 51 轮回填后 | `owner_scoped_cache` 命中，backend=redis |
| rewarmed_redis_hit_after_cold_restore | `true` |
| peak tokens / 预算 | 4127 / 5171，溢出 0 |
| median / p95 构建延迟 | 40.97ms / 112.35ms |
| 压缩尝试 / 事件 | 469 次 / 500 条 append-only，owner 隔离通过 |

50 轮（`real_mysql_50_turns_final_review_real_redis_20261118`）：同样 PASS，48/2 命中，peak 4153，回填命中 `true`。

## A2. 短 TTL 真实过期 → MySQL 冷恢复 → Redis 回填

`tests/test_context_storage.py::test_real_redis_short_ttl_expiry_restores_from_mysql_and_rewarms`（双门禁 `SMARTCS_CHECKPOINT_MYSQL_TEST=1` + `SMARTCS_CONTEXT_REDIS_TEST=1`）：`WorkingSetCache(ttl=1)` 实际等待 1.2s 让真实 Redis key 过期，验证：首次冷填充 → 立即命中 → TTL 过期 → `cold_event_store_restore`（含持久化近期历史）→ 回填后再次命中，全程 `backend=redis`。

配套修复：`context/manager.py::_load_working_set` 原本在 `cache.get()` 之前读取 `backend_status`，懒连接未建立时会误报 `process_fallback`；现改为在实际 get/put 之后读取，首轮构建即如实上报真实传输层。

## B. UserMemoryWorker（后台记忆 Worker）

### 生命周期

- `memory/user_memory_worker.py` 新增 `UserMemoryWorker`：由 FastAPI lifespan 启动/停止（`api/main.py`），应用关闭时优雅取消。
- 循环：每轮先 `pending_user_ids()`（持久库扫掠）→ 逐 owner `process_pending()`（有界：≤20 users × ≤20 candidates）。
- 退避：有工作时 2s 轮询；空闲指数退避 15s→60s 封顶（`asyncio.Event` 可被 stop 立即唤醒，无忙轮询，关闭无需等待睡眠周期）。
- 持久语义：claim 基于 DB 行 `claim_token + claim_until` 租约；崩溃后租约过期即可被下一轮扫掠回收；`mark_candidate` 以 claim_token CAS 保证并发 Worker 不重复应用。
- 恢复源是数据库 PENDING 行本身，不依赖 `asyncio.create_task`/内存队列——进程重启后无需新用户消息即可补处理。
- 失败只记录类型与计数（stats/日志均无记忆内容、无密钥），经 `release_candidate` 回 PENDING 延后重试；任何记忆失败都不触发模型/工具/业务重放。

### 聊天请求尾部

`agents/orchestrator.py::_process_user_memory` 收敛为**只做有界幂等 enqueue**（最多 2 次尝试）：校验真实提交的 owned USER_MESSAGE 源事件（含 PREPARED 边界、非 synthetic、内容精确匹配）后入队；不再 `await drain_pending()/process_pending()`。enqueue 失败仅告警，receipt/checkpoint 权威性不变。

### 新增测试

`tests/test_user_memory_worker.py`（9 项）：后台应用、重启后仅凭 PENDING 行恢复（无需新消息）、租约过期回收崩溃认领、两个并发 Worker 恰好一次应用、干净幂等启停、失败释放后重试成功、stats/日志不含记忆内容、构造参数有界校验、无可扫掠 API 的旧服务保持空闲。

`tests/test_context_integration.py`（+2 项）：聊天尾部永不等待候选应用（用永久阻塞的 drain 桩 + `wait_for` 超时护栏证明）；记忆 enqueue 失败不影响回复且重放同一请求零 LLM 重放（`llm.call_count` 不变）。

## C. 全量回归结果

| 项目 | 结果 |
|---|---|
| 全量确定性套件 | **512 passed, 37 skipped**（101.6s） |
| 真实 MySQL + 真实 Redis opt-in 套件 | **53 passed**（checkpoint+storage+memory，含短 TTL 集成） |
| 集成专项 | 18 passed（含 2 项新增 chat-tail 行为） |
| 100 轮 / 50 轮真实 Redis benchmark | PASS（见 A1） |
| `check_repository_readiness.py` | PASS（10 项） |
| `compileall` / `git diff --check` | 通过 |
| 保护文件 SHA256（38 个 RAG/评测/金数据） | 0 变更 |

## D. 本轮修改文件清单

- 新增：`memory/user_memory_worker.py`、`tests/test_user_memory_worker.py`
- 修改：`memory/user_memory.py`（协议+双仓库+服务新增可扫掠 `pending_user_ids`）、`agents/orchestrator.py`（移除内联 drain，仅 enqueue）、`api/main.py`（lifespan 启停 worker）、`context/manager.py`（cache backend 诊断在实际 IO 后读取）、`scripts/evaluate_context_engineering.py`（`--require-redis` 硬门禁 + Redis 汇总指标）、`tests/test_context_storage.py`（短 TTL 真实 Redis 集成测试）、`tests/test_context_integration.py`（+2 chat-tail 测试）、本证据文档与 `docs/context_manager_api.md`。

## E. 十二项需求对应（不变 + 增强）

前一轮已全部落实（见 git 历史工作区）；本轮将第 2 项（冷热路径）从"模拟/回退验证"升级为**真实 Redis 验证**，并使第 8 项（用户长期记忆）的延后处理成为真正的应用级后台 Worker 而非请求尾内联。

## F. 剩余限制（真实未解决项）

- Kimi/Moonshot 原生 tokenizer 仍未验证：所有 token 数为 tiktoken 估计（`tiktoken_estimate_not_native`），safety_margin 保留。
- benchmark 不调 LLM/业务写工具（基础设施验证）；在线模型质量与 SLO 数值未评测。
- 既有泛化 episode 无回填；历史标识符解析覆盖常见前缀格式；quote 上限 1200 字符。
- 多模态继续延期。
- （已解决）真实 Redis：本轮已在真实容器上验证 warm/cold/回填/TTL；此前"Redis 不可达"限制不再成立。

## 建议验收命令

```bash
# 1) 确认 Redis 容器健康（无需启动 API）
docker start smartcs-redis && docker exec smartcs-redis redis-cli ping

# 2) 全量确定性套件
OTEL_SDK_DISABLED=true EMBEDDING_BACKEND=hash python -m pytest -q

# 3) 真实 MySQL + 真实 Redis opt-in 套件（含短 TTL 集成测试）
REDIS_URL=redis://127.0.0.1:6379/0 OTEL_SDK_DISABLED=true EMBEDDING_BACKEND=hash \
  SMARTCS_CHECKPOINT_MYSQL_TEST=1 SMARTCS_CONTEXT_REDIS_TEST=1 \
  python -m pytest -q tests/test_checkpoint.py tests/test_context_storage.py tests/test_user_memory.py

# 4) 100 轮真实 Redis benchmark（硬门禁）
REDIS_URL=redis://127.0.0.1:6379/0 OTEL_SDK_DISABLED=true EMBEDDING_BACKEND=hash \
  python -m scripts.evaluate_context_engineering --turns 100 --real-mysql --require-redis \
  --output-root <new_dir>

# 5) 就绪检查
python scripts/check_repository_readiness.py
```
