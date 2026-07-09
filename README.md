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

```powershell
# 安装依赖
pip install -r requirements.txt

# 配置环境变量
# 本机运行复制 .env.example；Docker 运行复制 .env.docker.example
Copy-Item .env.example .env
Copy-Item .env.docker.example .env.docker
# 编辑 .env / .env.docker，填入自己的 OPENAI_API_KEY
# OPENAI_API_KEY=...
# OPENAI_BASE_URL=...
# MODEL_NAME=deepseek-v4-flash
# EMBEDDING_BACKEND=local
# EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5

# 抓取网页并生成 RAG Markdown
python -m scripts.fetch_knowledge_sources --config .\knowledge_sources\urls.yml --timeout 45 --min-clean-chars 200

# 构建 FAISS 向量库
python -m scripts.ingest_knowledge_base --kb-dir .\knowledge_base\generated --index-path .\vector_store\faiss_index --reset

# 运行测试（Mock LLM，无需 OPENAI_API_KEY）
pip install -r requirements-dev.txt
python -m pytest -q

# 启动服务
python -m api.main
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
├── knowledge_sources/           # 网页采集源、raw HTML、metadata、抽样审查
│   ├── urls.yml                 # 官方网页 URL 清单
│   ├── raw_html/                # 原始 HTML
│   ├── metadata/                # 每个 URL 的抓取和清洗元数据
│   ├── manifest.json            # 最近一次采集结果清单
│   └── review_samples.md        # 人工抽样校验预览
├── knowledge_base/              # RAG 入库文本
│   └── generated/               # 网页清洗后生成的 Markdown
├── scripts/                    # 运维脚本
│   ├── fetch_knowledge_sources.py # 抓取网页并清洗为 Markdown
│   └── ingest_knowledge_base.py   # 构建/更新 RAG 向量索引
├── tui/                        # 轻量终端聊天入口
├── requirements.txt
├── Dockerfile
├── .env.example                 # 本机运行环境变量示例
└── .env.docker.example          # Docker 运行环境变量示例
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

请求结束时 `api/main.py` 调用 `export_for_persistence` 将工作记忆快照持久化到短期记忆；服务重启后，`SupervisorNode` 会从最近的 `[wm_snapshot]` 恢复 `last_intent`、`accumulated_entities` 和 `turn_count`。

### RAG管线

完整5步RAG流程：Query改写 → 向量检索(Top-5) → LLM重排序(Top-3) → 上下文注入 → 生成回答。

### RAG向量库配置

长期记忆实现位于 `memory/long_term.py`，当前使用 FAISS 做本地向量索引，并把原文 chunk、来源文件、`doc_id`、`chunk_index` 等 metadata 一起保存，方便回答后追溯来源。

可选环境变量：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FAISS_INDEX_PATH` | `./vector_store/faiss_index` | FAISS索引与metadata保存位置 |
| `EMBEDDING_BACKEND` | `hash` | `hash`离线兜底；`local`/`sentence_transformers`走本地模型；`openai`/`remote`走远程API |
| `EMBEDDING_MODEL` | `BAAI/bge-small-zh-v1.5` | 本地embedding模型名；远程模式下可设为供应商支持的embedding模型 |
| `EMBEDDING_DIM` | `1536` | 远程embedding维度提示，本地模型会自动读取维度 |

工程建议：开发和测试可以用默认 `hash` 跑通流程；正式知识库优先使用本地 embedding 模型。当前 Apple RAG 知识库建议配置：

```env
EMBEDDING_BACKEND=local
EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
```

切换 embedding 模型后必须重新入库，因为旧 FAISS 向量的维度和语义空间不能复用。生产检索建议显式设置 `min_score`，避免弱相关 chunk 被送进大模型。

### RAG知识库入库流程

服务启动时不再写入演示知识。工程化流程是先构建向量索引，再启动 API 服务：

如果已经有整理好的 `.md` / `.txt` 文档，可以直接入库；如果来源是 HTML 网页，应先执行下一节的网页采集流程。

```powershell
python -m scripts.ingest_knowledge_base --kb-dir ./knowledge_base --index-path ./vector_store/faiss_index --reset
```

### 网页知识采集流程

如果原始资料来自 HTML 网页，先走可复现采集清洗，再入库向量化。URL 清单位于 `knowledge_sources/urls.yml`，采集脚本会保存：

| 路径 | 说明 |
|------|------|
| `knowledge_sources/raw_html/` | 原始 HTML，便于回溯和重新清洗 |
| `knowledge_base/generated/` | 清洗后的 Markdown，作为 RAG 入库输入 |
| `knowledge_sources/metadata/` | 每个 URL 的来源、抓取时间、清洗统计 |
| `knowledge_sources/review_samples.md` | 人工抽样校验用预览 |
| `knowledge_sources/manifest.json` | 本次采集结果清单 |

执行采集：

```powershell
python -m scripts.fetch_knowledge_sources --config ./knowledge_sources/urls.yml --timeout 45 --min-clean-chars 200
```

网页正文抽取链路为：

```text
httpx 抓取 HTML
→ trafilatura 抽取正文 Markdown
→ readability-lxml + html2text 兜底
→ 项目内置 HTMLParser 最后兜底
→ 站点噪声过滤与去重
→ metadata 记录实际 cleaner
```

`--min-clean-chars` 用于过滤登录页、JS 入口页、正文过短页面。被过滤的页面仍会保留 raw HTML 和 metadata，但不会写入 `knowledge_base/generated/`，避免污染向量库。

采集完成后，建议先查看 `knowledge_sources/review_samples.md`，抽样确认没有混入导航、页脚、Cookie 提示等噪声，再入库：

```powershell
python -m scripts.ingest_knowledge_base --kb-dir ./knowledge_base/generated --index-path ./vector_store/faiss_index --reset
```

如果要同时入库人工整理文档和网页清洗文档，可继续使用 `--kb-dir ./knowledge_base`。

当前 Apple 网页知识库最近一次采集结果为：18 个 URL 中 13 个页面生成可入库 Markdown，5 个正文过短页面被过滤。用 `BAAI/bge-small-zh-v1.5` 入库后，FAISS 索引为 512 维、95 个 chunk。

入库脚本会读取 `--kb-dir` 下的 `.md` 和 `.txt` 文件，切分 chunk，写入 FAISS 索引和同名 `.meta.json`。metadata 至少包含：

| 字段 | 说明 |
|------|------|
| `doc_id` | 文档级稳定 ID |
| `chunk_id` | chunk 级稳定 ID |
| `content_hash` | chunk 内容哈希 |
| `document_hash` | 原始文档内容哈希 |
| `source_path` | 原始文件路径 |
| `chunk_index` / `chunk_count` | chunk 在文档中的位置 |
| `updated_at` | 本次入库时间 |

重复执行入库脚本是幂等的：文件内容不变时不会重复写入；文件内容变化时，会替换该文件对应的旧 chunk 并重建索引。

验证当前 embedding 和 FAISS 维度：

```powershell
python -c "from dotenv import load_dotenv; load_dotenv(dotenv_path='.env'); import faiss; from memory.long_term import create_embedding_backend; b=create_embedding_backend(); idx=faiss.read_index('./vector_store/faiss_index'); print(type(b).__name__, b.dimension, idx.d, idx.ntotal)"
```

期望输出类似：

```text
SentenceTransformerEmbeddingBackend 512 512 95
```

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

## Terminal TUI

TUI 是一个 PowerShell 友好的轻量终端入口，只调用现有 FastAPI 接口，不改变后端 Agent 编排。

先启动后端服务：

```powershell
python -m api.main
```

再打开另一个 PowerShell，在 `python-impl` 目录启动 TUI：

```powershell
python -m tui.app
```

也可以指定后端地址和用户 ID：

```powershell
python -m tui.app --base-url http://localhost:8000 --user-id user_001
```

TUI 内置命令：

| 命令 | 说明 |
|------|------|
| `/help` | 查看命令帮助 |
| `/health` | 检查后端健康状态 |
| `/history` | 查看当前会话历史 |
| `/session` | 查看当前会话 ID |
| `/exit` 或 `/quit` | 退出 TUI |

### 测试

```bash
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"user_id": "user_001", "message": "理财产品A的收益率是多少？"}'
```

## Docker

Dockerfile 用于把 FastAPI 后端打包成可部署镜像。镜像内默认不打包真实 `.env` / `.env.docker`、`vector_store/`、raw HTML 和测试文件；运行时通过 `--env-file` 和 volume 挂载注入配置、向量库和模型缓存。

本机 Python 直接运行使用 `.env`；Docker 容器运行建议使用 `.env.docker`。不要提交真实 `.env` / `.env.docker`，仓库只提交 `.env.example` / `.env.docker.example`。

```powershell
Copy-Item .env.example .env
Copy-Item .env.docker.example .env.docker
```

两份配置最关键的区别是 FAISS 索引路径：

```env
# .env，本机 PowerShell 运行
FAISS_INDEX_PATH=./vector_store/faiss_index

# .env.docker，容器内部运行
FAISS_INDEX_PATH=/app/vector_store/faiss_index
```

原因是容器内部看不到 Windows 的 `D:\Workspace_for_Codex\...` 路径。启动容器时，下面这个 volume 会把宿主机的 `.\vector_store` 映射到容器里的 `/app/vector_store`：

```powershell
-v "${PWD}\vector_store:/app/vector_store"
```

所以容器内程序必须用 `/app/vector_store/faiss_index` 才能找到挂载进去的 FAISS 索引。

先在宿主机完成网页采集与向量入库：

```powershell
python -m scripts.fetch_knowledge_sources --config .\knowledge_sources\urls.yml --timeout 45 --min-clean-chars 200
python -m scripts.ingest_knowledge_base --kb-dir .\knowledge_base\generated --index-path .\vector_store\faiss_index --reset
```

构建镜像：

```powershell
docker build -t smart-cs-python .
```

运行服务（临时前台模式，关闭终端会影响查看日志）：

```powershell
docker run --rm `
  -p 8000:8000 `
  --env-file .env.docker `
  -v "${PWD}\vector_store:/app/vector_store" `
  -v smartcs-hf-cache:/home/app/.cache/huggingface `
  -v smartcs-st-cache:/home/app/.cache/sentence-transformers `
  smart-cs-python
```

推荐日常使用后台模式，并给容器固定命名：

```powershell
docker run -d --name smartcs-api `
  -p 8000:8000 `
  --env-file .env.docker `
  -v "${PWD}\vector_store:/app/vector_store" `
  -v smartcs-hf-cache:/home/app/.cache/huggingface `
  -v smartcs-st-cache:/home/app/.cache/sentence-transformers `
  smart-cs-python
```

说明：

| 配置 | 作用 |
|------|------|
| `-d --name smartcs-api` | 后台运行容器，并使用固定名称便于 start/stop/logs |
| `--env-file .env.docker` | 注入模型服务、embedding、FAISS路径等环境变量 |
| `-v "${PWD}\vector_store:/app/vector_store"` | 挂载宿主机已构建好的 FAISS 索引 |
| `smartcs-hf-cache` | 缓存 HuggingFace 模型文件，避免每次容器启动都重新下载 |
| `smartcs-st-cache` | 缓存 sentence-transformers 模型文件 |

服务启动后，直接访问：

| 地址 | 作用 |
|------|------|
| http://localhost:8000/health | 健康检查 |
| http://localhost:8000/docs | Swagger UI，可视化测试 API |

常用容器管理命令：

```powershell
docker ps
docker logs -f smartcs-api
docker stop smartcs-api
docker start smartcs-api
```

如果启动时出现 `port is already allocated`，说明 8000 端口已经被旧容器或本机进程占用。先查看正在运行的容器：

```powershell
docker ps
```

如果旧容器就是本服务，可以直接继续使用 `http://localhost:8000/docs`；如果需要重启，先停止旧容器：

```powershell
docker stop smartcs-api
```

如果容器不是固定名称，使用 `docker ps` 输出里的 `NAMES` 停止，例如：

```powershell
docker stop lucid_johnson
```

日志中如果出现 `localhost:4317` / `OTLP` / `Failed to export traces`，通常只是 OpenTelemetry 追踪收集器未启动，不影响 `/health`、`/docs` 和 `/api/chat` 使用。需要追踪时再单独启动 Jaeger 或 OTLP collector。
