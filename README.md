# Python 实现 — LangGraph + FastAPI

基于 LangGraph StateGraph 的多Agent智能客服系统，Python原生实现。

## 技术栈

| 组件 | 技术 |
|------|------|
| Agent编排 | LangGraph StateGraph + MemorySaver |
| HTTP框架 | FastAPI + Uvicorn |
| LLM调用 | LangChain ChatOpenAI |
| 向量检索 | FAISS |
| 短期记忆 | Redis (aioredis) |
| 追踪 | OpenTelemetry + Jaeger |
| 协议 | MCP 工具语义（HTTP `/api/tools`；内部 JSON-RPC 处理函数） |

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env 填入 OPENAI_API_KEY

# 启动服务
python -m api.main

# 运行测试（Mock LLM，无需 OPENAI_API_KEY）
pip install -r requirements-dev.txt
pytest
```

服务启动后访问 http://localhost:8000/docs 查看 Swagger UI。

## 项目结构

```
python-impl/
├── agents/                     # Agent实现
│   ├── supervisor.py           # Supervisor编排Agent（StateGraph核心）
│   ├── intent_router.py        # 意图路由Agent
│   ├── knowledge_rag.py        # RAG知识检索Agent
│   ├── ticket_handler.py       # 工单处理Agent
│   └── compliance_checker.py   # 合规审查Agent
├── memory/                     # 三层记忆系统
│   ├── working_memory.py       # 工作记忆（进程内存）
│   ├── short_term.py           # 短期记忆（Redis，30min TTL）
│   └── long_term.py            # 长期记忆（FAISS向量库）
├── mcp/                        # MCP工具协议
│   └── mcp_server.py           # 工具注册与调用（REST 暴露见 api/main.py）
├── tracing/                    # OpenTelemetry追踪
│   └── otel_config.py          # 追踪配置 + Agent装饰器
├── api/                        # FastAPI接口层
│   └── main.py                 # REST API入口
├── requirements.txt
├── Dockerfile
└── .env.example
```

## 核心特性

### Supervisor编排

LangGraph StateGraph构建有向图，编排顺序与 Java/Go 一致：

```python
graph.add_edge("supervisor_route", "intent_router")
graph.add_conditional_edges(
    "intent_router",
    route_after_intent,
    {
        "knowledge_rag": "knowledge_rag",
        "ticket_handler": "ticket_handler",
        "compliance_check": "compliance_check",
    },
)
graph.add_edge("knowledge_rag", "compliance_check")
graph.add_edge("compliance_check", "synthesize")
```

`supervisor_route` 读工作记忆并注入 `sub_results["_wm_context"]`；`intent_router` 负责 LLM 意图分类并写入 `state.intent`，完成后将本轮实体合并到工作记忆的 `accumulated_entities`。

### 工作记忆激活

工作记忆在单次请求内维护跨轮状态，通过 `sub_results["_wm_context"]` 通道注入 AgentState：

| 字段 | 写入时机 | 消费者 |
|------|---------|--------|
| `last_intent` | `intent_router_node` 完成后 | `intent_router.classify` 跨轮意图消歧 |
| `accumulated_entities` | 每轮实体合并（新覆盖旧） | `knowledge_rag` / `ticket_handler` 实体补全 |
| `turn_count` | 每轮递增 | 监控/调试 |

请求结束时 `api/main.py` 调用 `export_for_persistence` 将工作记忆快照持久化到短期记忆。

### RAG管线

完整5步RAG流程：Query改写 → 向量检索(Top-5) → LLM重排序(Top-3) → 上下文注入 → 生成回答。

### 两阶段合规审查

1. **规则引擎**（<2ms）：敏感词匹配 + PII检测
2. **LLM深度审查**（~600ms）：处理越权承诺、隐晦违规等规则无法覆盖的场景
3. 高风险直接拦截不走LLM，LLM失败安全降级为通过

### MCP工具

4个已注册工具（业务调用见 `ticket_handler` / 合规转人工；RAG 走 FAISS）：
- `order_query` — 订单查询（ticket_handler）
- `ticket_create` — 工单创建
- `knowledge_search` — HTTP 调试
- `risk_check` — 已注册，合规接入规划中

## API接口

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/chat` | POST | 聊天 |
| `/api/history/{session_id}` | GET | 对话历史 |
| `/api/tools` | GET | MCP工具列表 |
| `/api/tools/call` | POST | MCP工具调用 |
| `/api/metrics` | GET | 系统指标 |
| `/health` | GET | 健康检查 |

### 测试

```bash
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"user_id": "user_001", "message": "理财产品A的收益率是多少？"}'
```

## Docker

```bash
docker build -t smart-cs-python .
docker run -p 8000:8000 --env-file .env smart-cs-python
```
