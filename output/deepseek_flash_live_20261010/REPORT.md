# DeepSeek-V4.1-Flash 切换与真实问答（2026-10-10）

## 结论

正式 8001 API 与 Pi Harness 的 LLM 已切换为官方 **DeepSeek-V4.1-Flash**，API ID 为 `deepseek-flash`，OpenAI-compatible base URL 为 `https://api.deepseek.com/v1`。官方模型列表及真实 Chat Completions 请求均通过；真实 Pi transcript 的两次 LLM 消息都为 `deepseek-flash`，不是只改显示名称。

GPU FP32 重排、CPU Embedding、`SMARTCS_RERANK_MAX_CHARS=0` 保持。API/Harness 使用原镜像，只替换三个 LLM 配置字段；数据库、Redis 不重启；12 个生产索引文件 SHA256 保持一致。

## 实测：同一问题、各全新 Pi 会话

| 范围 | 之前 Kimi + GPU | DeepSeek 替换后首轮 | DeepSeek 热态复测 |
|---|---:|---:|---:|
| 完整 `/api/chat` | 18.0295 s | 17.8719 s | **4.7540 s** |
| 两次 LLM 消息区间合计，近似 | 11.089 s | **2.547 s** | **3.088 s** |
| `knowledge_search` 区间，近似 | 6.188 s | 14.127 s | **1.136 s** |
| 报告的 reasoning tokens | 414 | 139 | 121 |

- 首轮 LLM 近似区间减少 **77.03%**（约 4.35×），但完整问答只减少 **0.87%**：知识工具区间变长，抵消了模型收益。
- 热态完整问答 **4.75 秒**，比之前 18.03 秒少 13.28 秒（**73.63%**）。这包含检索/缓存热态变化，**不能全部归因于换模型**。
- Kimi 是先前那轮 GPU 刚启动后样本，不是当前同时间、同热态的严格 A/B；没有补 Kimi 热态样本，不能宣称已得到模型纯因果加速倍数。
- 不是只挑最快结果：首轮 17.87 秒和热态 4.75 秒原始记录全部保留，各只有一个样本，无生产 P95/SLA 结论。

LLM 首轮两次消息区间约 **1.370 + 1.177 秒**；热态约 **1.050 + 2.038 秒**。它们包含调用等待、输入处理、推理和输出，不是纯解码时间。工具区间包含 CPU Embedding、检索融合、GPU 重排及业务工具协议开销，不是纯 Cross-Encoder 时间。

首轮之后主机可用 RAM 采样约 **0.505 GiB**，说明环境内存紧张；但没有采样到完整问答期间的页入/缺页事件，也没有 RAG 内部分段计时，因此**不能把 14.13 秒确定归因于分页、CPU Embedding或某个具体环节**。热态复测只证明下一轮工具区间降为 1.14 秒。未停止用户其他程序、未调整页面文件。

## 为什么短问答仍会有 LLM 耗时

链路有两个串行模型调用：先决定工具参数，等待检索，然后根据资料生成答复。输入还包含系统提示、工具 schema、业务上下文与资料，而不只是用户问题；输出包含隐藏 reasoning tokens，而不只是可见答复。此前 11.09 秒并不等于“生成那段短文本用了 11 秒”，目前也没有分别测 provider 排队、网络、prefill 和 decode。

本轮保留 DeepSeek 官方默认 **thinking enabled / high**，没有关闭思考后冒称“单纯换模型”的收益。项目 SDK 注册的 `reasoning:false` 是能力元数据，不等于向服务端发送 `thinking:disabled`；官方默认思考仍发生，实际 usage 中可见 reasoning tokens。Pi SDK 已正确保留并回传 thinking tool-call 的 `reasoning_content`，两次真实工具续接均成功，无 HTTP 400。

## 答复与质量范围

固定请求：

```text
请只调用一次 knowledge_search，query 为“Apple 账户恢复等待时间，联系客服能否缩短等待”，top_k 为 3。
随后根据检索内容，用150字以内回答这个问题，并附官方来源链接。
只做知识查询，不执行其他工具或任何业务写操作。
```

三份样本都只有一次成功知识工具调用，没有写业务工具。query/top_k 完全一致，返回 Top3 chunk ID 和顺序一致：

```text
4e93c0b61f31fc9ba0084371
9b925299b53c5e4dfb82e9c8
831a8581615b3e86914f36be
```

核心事实一致：账户恢复可能需要数天或更久，Apple 支持不能缩短等待，72 小时指确认信息而不是保证恢复完成；官方来源均为 `https://support.apple.com/zh-cn/118574`。两轮 HTTP 200、`compliance_passed=true`。文字不逐字相同；模型有时未严格遵守 150 字篇幅约束，因此只记“核心事实/来源/只读工具验收”，不声称全部格式和全量客服质量验收通过。未做独立 holdout、写工具、并发或多轮记忆回归。

## 部署与边界

- 三个变动字段：`OPENAI_BASE_URL`、`OPENAI_API_KEY`、`MODEL_NAME`。其余运行环境逐键比对保持，包括用户/服务 JWT 密钥、MySQL、rollout、读工具超时和 GPU 配置。
- `.env` 与 `.env.docker` 的三个字段已私有更新（这些真实配置被 Git 忽略）；没有把密钥写入代码、README、报告或 public artifacts。
- 旧配置和容器环境备份在仓库根目录之外 `../.runtime/deepseek_flash_switch_20261010/`。私有 Compose override 保留实际旧部署配置，防止重新创建时无关配置漂移。不要公开该目录。
- 原 GPU API image ID：`sha256:94f89cbb9102d5af24d5c59cdd7548a7f1dd3aa2b0bbfec14ccdcfdfdd37dae2`；Harness image ID：`sha256:aea6683e7b9a140aeb28c415f003327830d67b452d16c387f4d541038ba8c8bf`。两个 image ID 均未变、没有重建镜像或升级 Pi SDK。
- 仅重建运行容器以刷新 Python 模型实例和 Harness 缓存的 agent handles，数据卷、逻辑挂载、端口保持，GPU参数仍在实际 PID 7 为 CUDA FP32，Embedding CPU。
- Docker API rollout 仍为 0%；沿用上一轮正常登录测试方式：8000 仅合法创建同一账号的 Pi 会话，8001 正常登录/所有权核对/聊天。没有绕过认证。8000 的独立旧进程及旧认证问题未修复，不应把它当作本次已更新的正式服务。
- 第一次切换实际健康，但验收脚本误以为 Harness 有 `dist/config/env.js`，其实际是 TSX `src/config/env.ts`。保护流程自动恢复原文件与容器模型配置，随后先验证正确探针路径再重做切换，全部通过。记录见 `deployment_probe_path_failure.json`、`rollback_probe_path.log`。这不是 DeepSeek 模型/密钥错误。
- 一个准备脚本字典键拼写错误导致 Python 解析失败，未执行任何修改；修正后准备成功。
- 本次为小型运行配置更换与实测，主 Agent 直接处理私有配置，不委派密钥，不作主要应用代码实现。没有新增运行单元测试或改动模型 loop；真实链路验收不是伪称完整测试套件通过。

## 验收证据

| 项目 | 结果 |
|---|---|
| 官方 `/models` | 200，包含 `deepseek-flash` |
| 实际 Chat Completions 小请求 | 200，返回 model=`deepseek-flash`，完成 OK；不算知识问答性能 |
| 两个容器的环境差异 | 恰好三个 LLM 字段 |
| 原镜像、逻辑挂载、端口、GPU请求 | 保持 |
| 实际 Harness 配置读取 | `src/config/env.ts` 得到官方 URL/模型，key 不打印 |
| 两轮实际 Pi LLM/tool/LLM 续接 | 成功，无 fallback/Faux、无工具错误 |
| 固定 query、Top3 及顺序、官方来源 | 保持 |
| 12 个生产索引文件哈希 | 保持 |
| API health / Harness ready | 200，ready=true，无 degraded |
| 操作脚本 `py_compile` / `git diff --check` | 通过（最终命令） |
| 公开产物中的密钥扫描 | 无密钥 |
| 生产 P95、并发、长期质量门禁 | 未测 |

正常容器 restart 会保持新的配置。重新执行本轮部署配置：

```bash
SMARTCS_GPU_IMAGE=smartcs-api:gpu-fp32-20261010 SMARTCS_RERANK_MAX_CHARS=0 \
  docker compose -f compose.yaml -f compose.gpu.yaml \
  -f ../.runtime/deepseek_flash_switch_20261010/.env.new-model.compose.json \
  up -d --no-deps --no-build --pull never smartcs-api smartcs-pi-harness
```

旧 GPU 部署产物中的 `.env.preserve-api.compose.json` 保留的是当时 Kimi 的完整环境，不应再直接用于当前 Flash 配置重建，否则会恢复旧模型字段。若需要回滚，应按本轮私有备份恢复 `.env`/`.env.docker` 与 `.env.rollback-model.compose.json`，不应只恢复某一个字段。

公开证据：`comparison.json`、两轮问答及 transcript summary、`deployment_result.json`、`final_verification.json`、模型与小请求预检、切换/回滚日志。关键操作脚本 `switch_model.py` 只允许从原 Kimi 配置切换，不允许在已部署 DeepSeek 上盲目重复执行。

## 官方来源

- 模型 canonical ID 与别名路由：[Your First API Call](https://api-docs.deepseek.com/)
- V4.1-Flash 发布与 `deepseek-flash`：[2026-09-10 release](https://api-docs.deepseek.com/news/news260910/)
- 默认 thinking enabled/high 与工具 reasoning_content 回传：[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)

官方返回和 transcript 可以证明本次使用 API ID `deepseek-flash`；它映射 V4.1-Flash 的依据是上述官方文档，不将 API ID误写成独立的 `deepseek-v4.1-flash`。
