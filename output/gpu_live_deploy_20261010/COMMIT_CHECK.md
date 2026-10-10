# 提交前检查补记（2026-10-10）

这是提交前验证记录，不改写 GPU/DeepSeek 部署报告的原始测量，也不代表新一轮生产部署或完整质量门禁。

## 范围与排除

提交本轮 GPU 设备策略、Docker/Compose override、离线设备 runner 与测试，以及 `output/gpu_rerank_eval_20261010/`、`output/gpu_live_deploy_20261010/`、`output/deepseek_flash_live_20261010/` 下未被 Git 忽略的公开证据。真实 `.env`、私有运行目录、模型/索引、日志与 Python 缓存不提交。既有 Phase10/Phase12 输出、`source_catalog.md` 和未跟踪的面试文档保留，未混入本次提交。

## 两项小防护

1. `rag/model_devices.py`：旧版 Torch 若没有 `set_float32_matmul_precision`，仍通过两项 backend flags 关闭 TF32，不因可选 API 缺失而阻断 auto/显式 CUDA FP32 模型初始化。现代 Torch 的 `highest` 调用保持。
2. `scripts/benchmark_reranker_devices.py`：在 `model_path`、输入读取、CLI 输入/输出 resolve 前拒绝显式 UNC/device 路径，避免先接触网络文件系统。此防护是文本路径检查，不声称能检测操作系统映射盘或用户创建的 junction/symlink，也不替代 OS 级网络隔离。

这两项使用最小修改，不调整评分、排序、批次、激活、GPU 精度、截断预算或任何原始测量。**没有重建正在运行的容器**；GPU 部署报告仍描述其当时镜像中的代码与哈希，提交中的旧 Torch 防护尚未重新打入该镜像。当前已测 Torch 2.11 的计算策略不因此改变。

## 本次验证

- 从 `0f0bc37691a7072f56755808ff451639232dbccc` 建立临时 detached worktree，只应用本轮候选文件；没有真实 `.env`，没有混入其他未提交文件。
- 先新增回归测试，在修复前候选源码上得到 **8 failed**：两项旧 Torch API 缺失用例、六项 UNC 在 filesystem read/stat/resolve 前拒绝的用例。全部使用 mock，没有接触网络共享或加载真实模型。
- 修复后在同一隔离工作副本运行以下离线子集，得到 **224 passed in 17.60s**：

```bash
python -m pytest -q \
  tests/test_rag_model_devices.py \
  tests/test_reranker_device_benchmark.py \
  tests/test_rag_rerank_truncation.py \
  tests/test_rag_rerank_safeguard.py \
  tests/test_rag_benchmark.py \
  tests/test_rag_tokenizer_ab.py \
  tests/test_api_startup_rag.py \
  tests/test_knowledge_rag.py \
  tests/test_long_term_memory_engineering.py
```

- `py_compile`、`git diff --check` 通过；离线 runner `--help` 与 Compose `config --quiet` 检查通过。
- 重新运行离线结果核验器，四份结果的输入、qrels、排名、指标、计时及设备证据通过；仅重新生成 comparison 的创建时间，未重跑推理或改写原始分数。两份 Torch build 文本只清理行末空白和末尾多余空行，构建字段与版本信息保持。
- 对候选公开文件扫描实际凭据和 provider/GitHub/JWT token 形态，未发现新泄漏。扫描只输出文件名/字段名，不输出值；真实配置仍 Git ignored。
- 本次没有运行完整 pytest/MySQL、LLM API 或 GPU 推理。先前报告的部署/实测验收保持历史口径，不冒称本次又完成全部线上验证。

## 已界定的使用范围

文档中的 GPU 安装路径为 `Dockerfile.gpu`，其中包含 `device_map` 所需的 accelerate；requirements-only 的手工原生 CUDA 安装不是已经验收的部署方案。注入模型的参数验证只证明权重设备/dtype，外部调用者自行负责其计算策略；生产路径不注入外部模型，离线 runner 独立关闭 TF32。runner 按报告中的冻结环境和输入使用，公开输出中的部署/观察器脚本是一次性实验助手，不作为通用生产 CLI 或进程管理器发布。
