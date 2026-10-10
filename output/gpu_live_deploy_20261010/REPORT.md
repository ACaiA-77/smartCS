# GPU 重排部署与真实问答对照（2026-10-10）

## 结论

**GPU 已进入真实业务链路，不再只是隔离评测。** `smartcs-api` 当前为 `smartcs-api:gpu-fp32-20261010`，重排模型在实际 uvicorn PID 7 上使用 `cuda:0 / torch.float32`；BGE-M3 Embedding 同一进程仍使用 CPU。API healthy、Harness ready。仅 API 被替换，Harness、Redis、MySQL 容器 ID 保持不变。

同一问题、同一检索参数、两个全新 Pi 会话，各一次成功完整问答：

| 计时范围 | CPU 部署前 | GPU 部署后 | 本轮减少 | 倍数 |
|---|---:|---:|---:|---:|
| `/api/chat` 完整响应 | 41.4362 s | 18.0295 s | 23.4067 s，56.49% | 2.298× |
| `knowledge_search` 工具时间差，近似 | 21.235 s | 6.188 s | 15.047 s，70.86% | 3.432× |
| 两次模型消息时间差合计，近似 | 19.299 s | 11.089 s | 8.210 s | 1.740× |

**不能把全部 23.41 秒减少归因于 GPU。** 约 15.05 秒减少发生在知识工具区间；本轮 LLM 消息区间也少了约 8.21 秒，报告的 reasoning tokens 从 903 变为 414。完整耗时是本轮真实观测，不是严格隔离的设备因果实验、生产 P95 或 SLA。

前后只调用一次 `knowledge_search`，参数完全相同，返回的 Top3 chunk ID 和顺序一致，回答均说明“账户恢复可能需要数天或更久，联系客服不能缩短等待”，引用相同官方 URL `https://support.apple.com/zh-cn/118574`。生成文本不逐字相同；没有证明全部查询或全部回答质量不变。

## 版本与实际设备

- 原 CPU 镜像：`sha256:ce0c6c24dfc500e2eb98f1f794a0486ee9f0033b1658f21882d7fe2de6934a03`。
- 最终 GPU 镜像：`sha256:94f89cbb9102d5af24d5c59cdd7548a7f1dd3aa2b0bbfec14ccdcfdfdd37dae2`。
- 回滚标签：`smartcs-api:cpu-before-gpu-20261010`，已从原实际 image ID 创建并核验。
- Python 3.12；Torch 从 `2.13.0+cpu` 变为 `2.11.0+cu128`。
- 其余应用版本保持：Sentence Transformers `6.1.0`、Transformers `5.18.0`、NumPy `2.5.3`、FastAPI `0.142.2`。没有安装 torchvision。
- 审查后的兼容例外：GPU 派生层将 setuptools `84.0.0` 固定为 `80.9.0`；新增 accelerate `1.15.0`、psutil `7.0.0`，以及 Torch CUDA/triton 支持依赖。已有应用依赖没有整体重新解析或升级，最终 `pip check` 通过。完整版本对照见 `package_comparison.json`。
- RTX 4060 Laptop GPU，驱动 `572.83`，CUDA wheel runtime `12.8`。
- GPU 预检禁用 TF32，实际训练好模型的加载和预测通过；实际 API 初始化也沿用同一 FP32 禁用 TF32 实现。
- 本轮部署为 **FP32**，不是 FP16、int8 或 ONNX；没有重新训练模型，也没有把远程 LLM 放到显卡上。

实际服务启动日志（不是另一个 `docker exec` Python 进程的参数）：

```text
RAG model startup pid=7 path=loaded model=BAAI/bge-m3 device=cpu dtype=torch.float32 parameter_count=567754752 verification=passed
RAG model startup pid=7 path=loaded model=BAAI/bge-reranker-v2-m3 device=cuda:0 dtype=torch.float32 parameter_count=567755777 verification=passed
```

通过 `/proc` 的真实 uvicorn cmdline 核对 PID 7。详见 `live_model_startup.txt`、`deployment_result.json`。单独容器中的 CUDA 矩阵/完整模型预检仅作为前置兼容验证，不冒充实际 API 的设备证明。

## 对照协议

- 用户明确授权部署 GPU 并实测真实问答；只替换 API，保留旧镜像回滚。
- 不改全局 Python、现有 Windows 隔离评测环境、系统页面文件；不重启其余服务、不清理用户镜像/数据。
- 固定模型 `BAAI/bge-reranker-v2-m3`，既有缓存 snapshot 为 `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`。
- GPU 部署明确设置 `SMARTCS_RERANK_DEVICE=cuda:0`、`SMARTCS_RERANK_DTYPE=fp32`、`SMARTCS_EMBEDDING_DEVICE=cpu`。
- **本轮 `SMARTCS_RERANK_MAX_CHARS=0`**，保持旧 phase11 不截断行为，不叠加 Phase 12 的 768 字符截断收益。模型原有 token 上限并未取消。
- 既有 `RAG_INDEX_ROOT=/app/artifacts/rag_jieba/production_indexes`、`global_corpus_v1`、CPU FAISS 与 BM25/RRF 配置保持。12 个生产索引文件的前后 SHA256 全部一致，不重建索引。
- 当前容器 rollout 保持 **0%**：不修改灰度或已有会话的 harness_version。本次使用合法创建的全新 Pi 会话，正常 `/api/chat` 根据其已存版本转发 Pi。
- 测试账号为已有 `demo-acceptance`。使用真实 Argon2 登录和 HttpOnly cookie，不伪造用户身份/JWT，不保存密码、token 或真实配置值到公开产物。
- 登录、会话创建及所有权检查在计时外；主计时从同步 HTTP `POST /api/chat` 开始，到完整响应读取完成，不是首 token 时间。
- 请求为只读知识查询；实际 transcript 验证只有一个非错误知识工具调用，没有订单/退款/工单写工具。
- CPU 为已有运行服务；GPU 为启动完成后的首次本测试问答，未人为给实际 API 的模型做预热。本轮不能称为严格相同冷/热态的统计对照。
- 工具时间差取 assistant toolCall entry 与 toolResult entry；模型时间差取 SDK message timestamp 与 assistant transcript entry timestamp，均为**近似区间**，不是 Python 检索细分 timer、纯重排时间或标准 provider HTTP span。
- 本轮有 Torch 版本、设备及打包支持依赖变化；不能宣称全部端到端差异仅来自设备。之前同版本离线设备对照是另一轮证据，不与本轮拼接统计。

问题与真实答复保存在 `before_cpu.json`、`after_gpu.json`。检索参数：

```json
{"query":"Apple 账户恢复等待时间，联系客服能否缩短等待","top_k":3}
```

Top3（前后顺序一致）：

```text
4e93c0b61f31fc9ba0084371
9b925299b53c5e4dfb82e9c8
831a8581615b3e86914f36be
```

## 实施过程中的偏差与修复

这些失败不计入成功问答耗时，未修改冻结的历史计划或 Phase 12 报告。

1. **旧 8000 宿主入口认证不兼容。** 最初转发返回 502，未调用模型。新登录 JWT 的 issuer/algorithm/有效期和账号一致，但其签名无法通过已部署容器密钥验证（`InvalidSignatureError`）；8000 token 在 Harness 认证探针返回 401，8001 正常登录的 token 返回输入校验 400。没有关闭或绕过认证。改用 8001 容器 API 登录/聊天；8000 仅按其原有 100% cohort 合法创建空 Pi 会话，两个入口都验证同一账号，8001 的会话列表再次核对所有权，真正聊天继续由服务端校验。8000 独立旧密钥问题未修复，勿混用两个入口的登录 cookie。失败记录见 `before_native_auth_failure.json`、`native_auth_signature_diagnosis.json`。
2. **裸本地 image ID 不能直接作为 BuildKit FROM。** 首次构建将 `sha256:...` 当作 registry image name/tag，pull 失败。改用已核验指向原实际 ID 的本地标签，不是处理 registry 凭据或拉取其他基础镜像。
3. **setuptools 约束。** Torch 2.11 CUDA 要求 `<82`，原 CPU 镜像为 84.0.0。仅在 GPU 层固定 80.9.0，其他应用约束不放宽。
4. **缓存预检挂载。** 第一轮离线模型预检只挂 HF cache，漏挂真正存有模型的 Sentence Transformers cache，因而 `LocalEntryNotFoundError`。补齐两个既有只读缓存，未下载模型或新建替代模型缓存。
5. **accelerate 必需。** 仅检查 HF dispatch 分支曾误判单一 device_map 不需要 accelerate；真实预检证明 TF 5.18 的 `check_and_set_device_map` 对单设备也强制依赖。新增固定 accelerate/psutil 后，完整 GPU 模型离线加载和真实预测通过；原巨大 CUDA 安装层复用，没有重新放宽应用依赖。
6. **首次矩阵预检非零、根因未确定。** 最初捕获子进程时直接 `check=True`，stderr 未保存，无法严谨判断原因。随后简化矩阵脚本和原完整脚本均复测通过，完整训练好模型及真实 API/问答另行验收。不能将该第一次错误解释为已确定的硬件或模型故障。后续产物完整保留 returncode/stdout/stderr。
7. **验收脚本触发三次自动回滚，非模型推理失败。** GPU 曾已 healthy 且参数日志通过，但首次 `/proc/cmdline` 探针把 NUL 放进 Windows 子进程参数，触发 `ValueError: embedded null character`。改为 `bytes([0])` 分隔，且在切服务前验证探针。后续挂载比较失败：先排除数组顺序影响，保存实际 diff 后确认 GPU/Desktop 将 `D:\\...` 写成同一驱动器的 `/run/desktop/mnt/host/d/...` 别名，而非数据迁移。只对这个明确前缀做规范化，其他挂载字段保持严格比较，另核验索引哈希。各失败守护流程恢复原 CPU，记录全部保留；最终 GPU 切换核验通过，未继续回滚。

## 验收

| 项目 | 结果 |
|---|---|
| 相关 Python 离线测试 | **271 passed**，一个既有 jieba/pkg_resources deprecation warning |
| 显式 CUDA 不可用拒绝回退、CPU fp16 拒绝、真实参数验证、WARNING 日志可见性 | 离线测试通过 |
| GPU 镜像 `pip check` | 通过 |
| 非授权应用依赖版本变化 | 无；完整 metadata 对照通过 |
| 容器 CUDA 矩阵、CrossEncoder import | 通过，后续完整脚本复测通过 |
| 完整真实 GPU reranker 离线预检 | 通过，567755777 参数、FP32 CUDA、实际分数有限 |
| 实际 API PID 7 的 Embedding CPU / reranker CUDA FP32 | 通过 |
| 端口、启动命令、运行用户、逻辑挂载定义 | 保持 |
| Harness/Redis/MySQL 容器 ID | 保持 |
| 生产索引 12 个文件 SHA256 | 全部保持 |
| CPU/GPU 两个正常认证 Pi 问答 | 均 200、compliance_passed=true、各一次知识工具 |
| 同一 query/top_k、Top3 chunk 顺序、官方来源及核心事实 | 保持；未证明全量生成回答等价 |
| 最终 API health / Harness health / Harness ready | 均 200，ready=true、无 degraded warning |
| 独立 holdout、并发、生产 P95、长期 GPU 稳定性 | 未测，不作准入/SLA 承诺 |

## 运行与回滚

当前 GPU API 容器 `restart` 会保留其设备请求、镜像和环境。重新执行 Compose 时必须带 GPU override；只用基础 Compose 会选择 CPU/default 镜像配置。本轮使用了原运行 API 环境的私有快照，以避免重建时无关凭据/配置漂移；位于仓库及 Docker build context **之外**的 `../.runtime/gpu_live_deploy_20261010/`，不要公开或提交这些文件。公开产物只保存路径和变动键名。

复现当前配置（仓库根，Git Bash）：

```bash
SMARTCS_GPU_IMAGE=smartcs-api:gpu-fp32-20261010 SMARTCS_RERANK_MAX_CHARS=0 \
  docker compose -f compose.yaml -f compose.gpu.yaml \
  -f ../.runtime/gpu_live_deploy_20261010/.env.preserve-api.compose.json \
  up -d --no-deps --no-build --pull never smartcs-api
```

回滚原 CPU（**不带 GPU override**）：

```bash
SMARTCS_IMAGE=smartcs-api:cpu-before-gpu-20261010 \
  docker compose -f compose.yaml \
  -f ../.runtime/gpu_live_deploy_20261010/.env.preserve-api.compose.json \
  up -d --no-deps --no-build --pull never smartcs-api
```

`deploy_api.py` 为本轮带回滚的切换控制器，包含实际 PID、环境变动键、逻辑挂载及其他服务未重启检查。它要求切换前仍是原 CPU 镜像，不是当前 GPU 上的盲目重复执行器。尚未将任何真实 env 写入仓库，也未 commit/push。

## 子任务配置记录

- Python 设备实现：session `01a12508-e973-7562-8f16-b591653ba4b8`；请求 `functions.Agent / openai-codex/gpt-6.1-sol / high`；回报 `PI_MODEL=gpt-6.1-sol`、`PI_REASONING_LEVEL=high`。实际 provider 未在最终回报中独立列出，effective tool 未核实，标为 unknown，不把请求项视为实际已验证项。
- Docker 配置与兼容返修：session `01a12508-e992-7562-8f16-b592b41f5ea3`；请求 `functions.Agent / openai-codex/gpt-6.1-sol / medium`；观测回报 `PI_PROVIDER=openai-codex`、`PI_MODEL=gpt-6.1-sol`、`PI_REASONING_LEVEL=medium`，effective tool 未核实，unknown。
- 两个子任务及返修均回收。真正构建、前后问答、切换、回滚与独立核验由主 Agent 完成。

主要原始证据：`comparison.json`（含关键产物 SHA256）、两份问答及 transcript summary、`deployment_result.json`、`package_comparison.json`、`mount_comparison.json`、索引前后哈希、`final_service_verification.json`、各构建/预检/回滚日志。
