# SmartCS Final Closeout Plan — Repository + Operational Readiness

## 0. 本轮目标与边界

当前 Pi Harness 主体迁移已经完成，本轮**不再新增 Agent 架构**，只做最终工程收口。

### 本轮要完成

```text
A. GitHub / Repository 收口
B. Readiness Probe
C. Memory Outbox 运行指标
D. Memory Outbox 人工恢复入口
E. 本地运行形态收敛
F. 文档与测试同步
```

### 本轮明确不做

```text
- 不做 RAG 性能优化
- 不更换 CrossEncoder
- 不修改 Recall / MRR / nDCG
- 不做 Python → TypeScript 全量迁移
- 不增加 Subagent
- 不把全部 Tool MCP 化
- 不实现多实例 distributed lease
- 不重写 WRITE / ExecutionLedger / PendingAction
- 不升级 Pi 1.0.1
```

原则：

> **只补齐仓库交付形态和运维闭环，不再改变核心架构。**

---

# Phase 1：GitHub 仓库收口

## 1.1 当前问题

当前本地实际上是两个独立 Git 仓库：

```text
D:\Workspace_for_Codex\project005_SmartCS\python-impl
  └─ origin = https://github.com/ACaiA-77/smartCS.git

D:\Workspace_for_Codex\project005_SmartCS\pi-harness
  └─ 独立 .git
  └─ 无 remote
```

因此当前 GitHub 上的 `smartCS` 仓库不能完整呈现最终架构。

这对简历有直接影响：

```text
面试官打开 GitHub
↓
看到 Python Business Runtime
↓
却看不到真正的 Pi Agent Harness
```

这是目前优先级最高的交付问题。

---

## 1.2 最终仓库形态

**不要重新建一个全新的 Git 根目录，也不要重写 Python 仓库历史。**

推荐直接以当前：

```text
python-impl/
```

这个已有 `smartCS.git` Git 根目录作为最终仓库根。

把 Pi Harness 纳入：

```text
smartCS/
├─ pi-harness/
│  ├─ src/
│  ├─ tests/
│  ├─ skills/
│  ├─ package.json
│  ├─ package-lock.json
│  ├─ Dockerfile
│  └─ README.md
│
├─ api/
├─ internal_api/
├─ rag/
├─ memory/
├─ mcp/
├─ refunds/
├─ tickets/
├─ platform_db/
├─ tests/
├─ docs/
├─ compose.yaml
├─ Dockerfile
└─ README.md
```

即：

> **一个 GitHub 仓库，一个项目，两套 Runtime。**

不要创建：

```text
smartCS-python
smartCS-pi
```

两个公开仓库。

用户简历只需要一个 GitHub 地址。

---

## 1.3 Pi Harness 纳入规则

从当前 sibling：

```text
../pi-harness
```

纳入当前 Git 根目录：

```text
./pi-harness
```

只带入真正需要版本控制的文件。

### 必须纳入

```text
src/
tests/
skills/
docs/
scripts/
package.json
package-lock.json
tsconfig.json
vitest.config.ts
Dockerfile
README.md
PHASE*.md
```

### 严禁纳入

```text
.git/
node_modules/
.runtime/
*.log
本地 session transcript
临时测试数据库
credentials
.env
API key
token
```

原来的 sibling `pi-harness` **暂时不要删除**。

先完成：

```text
copy/import
→ 修路径
→ 测试
→ Git diff review
→ 最终验收
```

之后才能决定是否删除旧目录。

---

# Phase 2：修复 Monorepo 路径

Pi Harness 进入当前 Git 根后，所有：

```text
../pi-harness
../python-impl
```

之类的 sibling 假设都必须清理。

新的逻辑应该是：

```text
repo root
├─ pi-harness
└─ Python Business Runtime files
```

重点检查：

```text
compose.yaml
pi-harness/src/config/env.ts
pi-harness/tests/helpers/*
pi-harness/README.md
README.md
docs/architecture.md
docs/runtime-boundaries.md
docs/recovery.md
scripts/*
Dockerfile
```

例如 Compose 当前：

```yaml
build:
  context: ../pi-harness
```

进入单仓库后应调整为类似：

```yaml
build:
  context: ./pi-harness
```

Pi 读取 Python `.env`、migration、测试 helper 等路径也必须统一更新。

---

# Phase 3：重写 GitHub 根 README 的入口定位

最终根 README 第一屏应该让人直接知道：

```text
SmartCS
↓
Pi Harness
+
Python Business Runtime
```

而不是第一眼看起来像一个 Python-only 项目。

建议 README 开头结构：

```text
# SmartCS

面向知识咨询与电商售后的智能客服 Agent 系统。

Architecture

Browser
   ↓
Pi Harness / TypeScript
   ↓
Internal HTTP / MCP
   ↓
Python Business Runtime
   ↓
RAG / ToolExecutor / ExecutionLedger / Domain DB
```

随后明确：

```text
pi-harness/
→ Agent Runtime

其余 Python 模块
→ Business Runtime
```

不要再让：

```text
ChatOrchestrator
IntentRouter
```

成为 README 第一层架构。

Legacy 可以保留，但只能作为：

> Historical / Legacy Compatibility Path

---

# Phase 4：补 Harness `/ready`

## 4.1 保留现有 `/health`

当前：

```text
GET /health
```

继续作为 **Liveness Probe**。

语义：

> Node 进程和事件循环还活着。

它不负责检查依赖。

不要把 `/health` 做得很重。

---

## 4.2 新增 `/ready`

新增：

```text
GET /ready
```

作为真正的 Readiness Probe。

### 必查项

#### ① Harness MySQL

执行最轻量检查：

```sql
SELECT 1
```

证明：

```text
agent_run_receipt
memory_source_event
等持久化层可连接
```

不跑 migration，不做写入。

#### ② Python Business Runtime

检查 Python Runtime 是否响应。

建议增加轻量内部 readiness：

```text
GET /internal/ready
```

不要用真实业务 Tool 做 readiness。

Python 返回至少：

```json
{
  "ok": true,
  "platform_db": true,
  "tool_runtime": true,
  "memory_runtime": true
}
```

不要：

```text
调用 LLM
执行 RAG
执行订单
执行退款
```

Readiness 必须便宜。

#### ③ MCP Gateway

仅当：

```text
SMARTCS_KNOWLEDGE_TRANSPORT=mcp
```

时检查 MCP Gateway。

如果：

```text
transport=http
```

则 MCP 完全不参与 readiness。

建议 MCP Gateway 增加：

```text
GET /health
```

其语义：

```text
ASGI 已启动
MCP Server 已初始化
Retriever 已初始化完成
```

不需要真正发一次搜索。

---

## 4.3 `/ready` 返回格式

建议：

```json
{
  "ready": true,
  "checks": {
    "mysql": {
      "ok": true
    },
    "business_runtime": {
      "ok": true
    },
    "mcp": {
      "enabled": false,
      "ok": true
    }
  }
}
```

依赖异常：

```text
HTTP 503
```

例如：

```json
{
  "ready": false,
  "checks": {
    "mysql": {
      "ok": false
    },
    "business_runtime": {
      "ok": true
    }
  }
}
```

不要把：

```text
API key
内部 URL
JWT
SQL 错误栈
```

返回给客户端。

---

# Phase 5：Compose 改用 Readiness

当前 Compose Harness healthcheck 如果只打 `/health`，只能说明进程还活着。

改为：

```text
/ready
```

用于 Ready 判断。

但保留：

```text
/health
```

给人工和进程 liveness 使用。

最终语义明确：

```text
/health
= 我活着

/ready
= 我现在可以正常服务用户
```

---

# Phase 6：Memory Outbox 运维指标

当前 Outbox 的功能已经正确：

```text
completed
→ pending
→ enqueue
→ done
```

并已经验证：

```text
18 pending
→ 18 done
```

现在只补**观测能力**。

不要改变它的业务语义。

## 6.1 Durable 指标

在 `ReceiptStore` 增加一个只读聚合查询，例如：

```text
memoryOutboxStats()
```

返回至少：

```text
pending_count
failed_count
oldest_pending_age_seconds
max_pending_attempts
```

可选增加：

```text
done_count
```

但不要每次扫描整个大表。

确保：

```text
memory_enqueue_status
updated_at
```

现有索引能支持查询。

## 6.2 Dispatcher 进程指标

`MemoryOutboxDispatcher` 自己记录：

```text
dispatch_pass_total
delivered_total
delivery_failure_total
parked_total

last_dispatch_at
last_success_at
last_error_at
```

这些只是 Observation，不是业务权威状态。

进程重启归零也没关系。

真正权威的是数据库中的：

```text
pending / done / failed
```

---

# Phase 7：增加内部 Outbox 状态接口

新增受 Service Auth 保护的：

```text
GET /internal/ops/memory-outbox
```

不能公开匿名访问。

返回示例：

```json
{
  "durable": {
    "pending": 0,
    "failed": 0,
    "oldest_pending_age_seconds": 0,
    "max_attempts": 0
  },
  "dispatcher": {
    "passes": 128,
    "delivered": 42,
    "delivery_failures": 3,
    "parked": 0,
    "last_dispatch_at": "...",
    "last_success_at": "...",
    "last_error_at": null
  }
}
```

这个接口只观察：

```text
不修改 receipt
不 retry
不执行 LLM
不执行 Tool
```

---

# Phase 8：Readiness 不要被普通 Memory Backlog 拖死

下面这种状态：

```text
memory pending = 10
```

不能直接导致：

```text
/ready = 503
```

因为聊天、订单查询、退款和 RAG 可能完全正常。

Memory 是异步能力。

因此建议：

```text
pending > 0
→ ready=true
→ degraded=true
```

而不是停止接收用户请求。

例如：

```json
{
  "ready": true,
  "degraded": true,
  "warnings": [
    "memory_outbox_backlog"
  ]
}
```

真正导致 `503` 的应主要是：

```text
MySQL 不可达
Business Runtime 不可达
MCP transport=mcp 且 MCP Gateway 不可达
```

---

# Phase 9：给 failed Outbox 增加人工 Replay

现在：

```text
maxAttempts = 5
↓
failed
```

之后就永久停住。

这是正确的自动策略：

> 毒消息不能无限自旋。

但应该有**人工恢复能力**。

增加 CLI，例如：

```text
npm run outbox:status
```

输出：

```text
pending: 3
failed: 1
oldest pending: 63s
```

以及：

```text
npm run outbox:retry -- --receipt-id 123
```

或者：

```text
npm run outbox:retry -- --failed --limit 10
```

行为只能：

```text
failed → pending
memory_attempts → 0
```

然后交回正常 Dispatcher。

CLI 自己：

```text
不能直接调用 memory enqueue
不能调用模型
不能调用 Tool
```

这样恢复路径仍只有一条：

```text
DB state
→ Dispatcher
→ Python
```

---

# Phase 10：改 Retry 节奏

当前：

```text
5 秒
5 秒
5 秒
5 秒
5 秒
→ failed
```

Python Runtime 短暂重启几十秒就可能把消息 park。

建议做简单退避，不需要复杂队列。

例如：

```text
attempt 0 → 5s
attempt 1 → 15s
attempt 2 → 30s
attempt 3 → 60s
attempt 4 → 300s
```

达到 max attempts：

```text
failed
```

但注意：

> 不要为了实现 backoff 引入 Redis Queue、Kafka、Celery、BullMQ。

当前数据库 outbox 已经足够。

可以增加类似：

```text
next_memory_attempt_at
```

让 SQL 只选择：

```sql
memory_enqueue_status = 'pending'
AND next_memory_attempt_at <= NOW()
```

如果 Claude 评估认为为这一个需求新增列不划算，也可以保持固定 interval。

这项属于推荐优化，不是阻塞项。

---

# Phase 11：修两个小一致性问题

## 11.1 System Prompt

当前固定身份描述：

> 可以帮你查询订单、处理退款、创建工单和检索服务政策。

但 writes off 时下面又说不能退款/建单。

改成中性：

> **可以帮你处理订单、退款、工单和服务政策相关问题。**

具体是否能执行 WRITE，由后面的动态 Tool Surface 说明。

## 11.2 Outbox 术语

当前文档中的：

```text
exactly once
恰好一次
```

改掉。

因为真实语义是：

```text
HTTP delivery
→ at-least-once

Python candidate write
→ idempotent

最终业务效果
→ effectively-once
```

推荐统一写：

> **At-least-once delivery + idempotent consumer，保证重复投递不会产生重复记忆结果。**

不要宣称网络投递 exactly-once。

---

# Phase 12：本地运行形态收敛

当前实际同时运行：

```text
127.0.0.1:8971
→ native Node Harness

127.0.0.1:8972
→ Docker Harness
```

而当前设计明确：

```text
Pi Harness = single instance
```

因此验收结束后必须选择一个。

建议最终开发 / Demo 统一：

```text
Docker Compose
```

即：

```text
smartcs-api
smartcs-pi-harness
smartcs-redis
smartcs-checkpoint-mysql
可选 smartcs-mcp-gateway
```

停掉 native Harness。

但在测试全部结束前不要随便杀当前环境，先确认哪个实例承担当前验收流量。

---

# Phase 13：GitHub / CI 验收

Pi Harness 纳入 GitHub 后，当前 Python CI 不能再完全忽略它。

至少增加一个 Node job：

```text
Node 22
npm ci
npm run typecheck
npm test
```

工作目录：

```text
pi-harness/
```

重型 MySQL / crash matrix 如果 GitHub Runner 配置成本较高，可以暂时不全部跑。

但至少确保：

```text
TypeScript compile
纯单测
不依赖本机状态的 Harness tests
```

进入 CI。

Python 原 CI 保持。

最终：

```text
Python CI
+
Pi Harness CI
```

才能与“一个完整项目仓库”的定位一致。

---

# Phase 14：验收清单

Claude 完成后必须给出逐项证据。

- [ ] `smartCS` Git 根能看到 `pi-harness/`
- [ ] `pi-harness/.git` 没有被嵌套进去
- [ ] `node_modules/.runtime/.env` 没有进入 Git
- [ ] Compose 路径全部适配单仓库结构
- [ ] 根 README 第一屏就是最终 Pi 架构
- [ ] `npm ci`
- [ ] `npm run typecheck`
- [ ] Phase 10 Outbox 5/5
- [ ] Tool Surface 9/9
- [ ] MCP Transport 4/4
- [ ] `/health` 仍然是轻量 liveness
- [ ] `/ready` MySQL 正常时 200
- [ ] MySQL 不可达时 `/ready` 503
- [ ] Python Runtime 不可达时 `/ready` 503
- [ ] HTTP knowledge transport 下不依赖 MCP
- [ ] MCP transport 下 Gateway 不可达时 `/ready` 503
- [ ] `/internal/ops/memory-outbox` 受内部认证保护
- [ ] 能看到 pending / failed / oldest age
- [ ] failed outbox 可人工重新置为 pending
- [ ] 人工 replay 不执行 LLM / Tool
- [ ] 文档不再宣称 exactly-once delivery
- [ ] writes-off Prompt 无能力自相矛盾
- [ ] 最终只运行一个 Pi Harness 实例
- [ ] GitHub Actions 至少验证 Python + Pi 两侧基本测试

---

# Claude 执行时的约束

这轮特别注意：

> **不要顺手重构。**

允许改的东西只有：

```text
仓库目录与路径
README/docs
Compose
Pi /ready
Python internal readiness
MCP gateway health
Memory Outbox metrics / admin
CI
对应测试
```

不要因为发现：

```text
旧代码不好看
目录能重命名
接口还能抽象
类还能拆
```

就扩大范围。

---

# 最终 Done 条件

这一轮真正结束时，我们应该得到：

```text
GitHub
  ↓
一个完整 SmartCS 项目
  ├─ Pi Agent Harness
  └─ Python Business Runtime

Runtime
  ↓
/health   = 活着
/ready    = 能服务

Memory Outbox
  ↓
能恢复
能观察
能告警
失败能人工重新投递
```

做到这里，**SmartCS 工程层就应该正式冻结**。

之后除了明确 bug，不再继续加架构，直接转到简历和面试准备。
