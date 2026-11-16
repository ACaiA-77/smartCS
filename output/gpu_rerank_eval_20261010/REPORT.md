# RTX 4060 重排设备对照实测

## 结论

现有 RTX 4060 Laptop 8 GB 足以运行当前 `BAAI/bge-reranker-v2-m3`，无需先换模型或显卡。本轮固定输入下，热态重排 P50 从 CPU 的 **9.321 秒**降到 GPU FP32 的 **0.415 秒**、GPU FP16 的 **0.143 秒**；对应约 **22.5 倍**和 **65.2 倍**加速。两种 GPU 模式均通过本轮速度、回归质量和排序相关性门禁。

这是共享笔记本上的离线重排实验，**不是完整问答提速、生产 P95 或上线验收**。FP16 存在少量排序变化，不能称数值精确等价。当前业务服务仍是原来的 CPU 部署。

## 冻结环境与测量协议

- 日期：2026-10-10；代码基线：`0f0bc37691a7072f56755808ff451639232dbccc`。
- GPU：NVIDIA GeForce RTX 4060 Laptop GPU，8188 MiB；驱动 572.83。
- CPU：AMD Ryzen 7 8845H；模型 intra-op 线程固定为 8。
- Python 3.13.2；sentence-transformers 5.6.0；Transformers 5.13.0；NumPy 2.2.4。
- GPU 环境：`../.venv-smartcs-gpu-eval-20261010/`，PyTorch `2.11.0+cu128`、torchvision `0.26.0+cu128`。
- CPU 对照环境：`../.venv-smartcs-cpu-control-20261010/`，PyTorch `2.11.0+cpu`、torchvision `0.26.0+cpu`。
- 两个 2.11 wheel 的源代码 commit 都是 `70d99e998b4955e0049d13a98d77ae1b14db1f45`；MSVC、MKL、MKL-DNN 版本相同，CPU/CUDA 构建标签不同。详见 `cpu_torch_build.txt`、`gpu_torch_build.txt`。
- 重排模型使用已有本地 snapshot：`953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`；全部真实推理验证 567,755,777 个参数、实际 dtype/device。强制离线，不调用 LLM。
- 固定文档侧 **768 Unicode 字符**预算，调用现有 `truncate_for_rerank`；query 不截断。这不是 token 上限。
- 输入：60 条真实查询，apple_support / agent_engineering 各 30，交错排列；真实双域 dense + global BM25 + RRF top20。95 条普通 chunk-ID qrels 全部保留，包括未进入候选的正例。
- 主机可用 RAM 不足以满足准备阶段保守的 6 GiB CPU 模型预加载门槛，因此临时用一个共享的 GPU FP32 BGE-M3 实例生成候选，完成后进程退出。没有在计时时运行 Embedding，更没有修改生产 Embedding 设备。准备过程峰值 GPU allocated 约 2.125 GiB。
- CPU/GPU 全部使用同一份冻结输入；其 SHA256：`285f314f1e01d362d13c8d173af5a063e247b63227fd1fb9e861170b358a6e03`。
- 计时：冻结输入前 12 条，各域 6 条，semantic 6 / lexical 5 / confusing 1；每条取 RRF top20 的前 9 个候选，固定 `predict(batch_size=9)`，重复 2 轮，共 24 次。
- 第一次预测单独记录；随后预热 3 次，均不进入热态统计。GPU FP32 禁用 TF32，计时前后 CUDA synchronize。
- 计时范围是 `predict()`，含 tokenizer、设备传输和输出转为 CPU 数组；不含排序、指标计算、检索、模型加载或 LLM 生成。
- P50/P95 采用 nearest-rank；24 次中的 P95 是第 23 个有序值。这不是稳定的生产长尾统计。
- 质量：全部 60 条查询，每条评分 20 个候选，仍固定 batch_size=9；按 `(-score, chunk_id)` 排序，使用项目的 Recall@10 / MRR@10 / graded nDCG@10 宏平均。

## 热态重排结果

| 模式 | P50 | P95 | 相对本轮 CPU 的 P50 倍数 | GPU 峰值 allocated / reserved |
|---|---:|---:|---:|---:|
| CPU FP32，Torch 2.11 | 9321.17 ms | 10073.28 ms | 1.0× | — |
| GPU FP32，Torch 2.11 | 414.97 ms | 445.04 ms | 22.5× | 2.327 / 2.814 GiB |
| GPU FP16，Torch 2.11 | 143.06 ms | 152.49 ms | 65.2× | 1.172 / 1.297 GiB |

GPU 显存计数从模型加载前重置，覆盖首次预测、预热、计时和全部质量评测；这是该进程的 PyTorch allocator 峰值，不是整张显卡含桌面等其他进程的总占用。未测两个模型同时驻留 GPU 的场景。

冷启动加载调用（含首次导入 sentence-transformers，不含 Python 启动/torch 首次导入）分别为 CPU 16.25 秒、GPU FP32 22.20 秒、GPU FP16 19.17 秒。GPU 首次预测分别约 1.17 秒、0.79 秒；**不能拿 0.143 秒代表冷启动请求**。

## 质量与排序差异

| 模式 | Recall@10 | MRR@10 | nDCG@10 |
|---|---:|---:|---:|
| CPU FP32 | 0.916667 | 0.809881 | 0.802828 |
| GPU FP32 | 0.916667 | 0.809881 | 0.802828 |
| GPU FP16 | 0.916667 | 0.809881 | 0.802828 |

不仅汇总相同，两种 GPU 模式的 **60 条逐查询指标也全部与 CPU 一致**。

| 对 CPU FP32 的比较 | GPU FP32 | GPU FP16 |
|---|---:|---:|
| 每查询最低 Spearman | 0.999624 | 0.998120 |
| Spearman 均值 | 0.999994 | 0.999862 |
| Top1 相同 | 60/60 | 60/60 |
| Top3 集合相同 | 60/60 | 60/60 |
| Top3 顺序相同 | 60/60 | 59/60 |
| Top10 集合相同 | 60/60 | 60/60 |
| 全部 20 个候选的完整排序相同 | 60/60 | 53/60 |
| 分数逐值精确相同 | 0/60 | 0/60 |
| 最大绝对分数差 | 0.000004590 | 0.004298866 |

FP16 的 Top3 顺序差异出现在 `apple_010`，第 2/3 名交换，集合未变。完整排序变化的查询为 agent_005、agent_006、apple_010、apple_013、agent_014、agent_016、agent_028。

分数采用生产默认 Sigmoid。相关性不是准确率；FP32 完整排序相同也不等于逐值精确相同。FP16 的 Top3 顺序变化是否影响生成回答，本轮没有验证。

### 本轮实验门禁

| 验收项 | GPU FP32 | GPU FP16 |
|---|---|---|
| 重排 P50 ≤ 2 秒 | 通过 | 通过 |
| Recall@10 回落 ≤ 1 个百分点 | 通过，回落 0 | 通过，回落 0 |
| MRR@10 相对回落 ≤ 2% | 通过，回落 0 | 通过，回落 0 |
| nDCG@10 相对回落 ≤ 2% | 通过，回落 0 | 通过，回落 0 |
| 参考历史排序标准：逐查询 Spearman ≥ 0.95 | 通过 | 通过 |

全部比较及逐查询证据见 `comparison.json`。门禁通过仅针对本轮历史回归集合，不是生产准入。

## 原有 Torch 2.13 CPU 抽样核验

全局原有 `2.13.0+cpu` 保持不变，另对同一冻结输入前 12 条跑 1 轮计时和 12 条质量核验：P50 **9072.72 ms**、P95 **9814.59 ms**（n=12，P95 等于最大值）。

与 Torch 2.11 CPU 的相同 12 条质量结果比较，完整排序全部一致，Spearman 全部为 1，最大绝对分数差约 `5.96e-8`，并非分数逐值精确相等。这支持此次主要收益来自 GPU，而不是仅换 Torch 版本；但这只是重排抽样，不包含 Embedding/召回全链路版本回归，也不能用 12 条质量汇总去比较完整 60 条。

## 过程偏差与资源限制

1. 继承全局 torchvision 0.28 时与隔离的 Torch 2.11 不匹配，模型框架导入失败。只在隔离环境内安装匹配的 torchvision 0.26，随后真实 CrossEncoder 导入和推理均通过。
2. 初始同一个 CUDA wheel 的 CPU 对照过程中，观察脚本 `psutil.children()` 的全系统进程快照触发 Windows `WinError 1455`。观察器退出时模型已经加载，但评测仍在运行；主 Agent 校验命令行后仅终止了本次评测的两个进程，没有终止用户服务。该尝试未产出完整分数，不纳入统计；见 `cpu_cubuild_aborted.json`、对应日志。
3. 最终 CPU 对照改为同一 2.11 源版本的 CPU-only wheel，减少不必要的 CUDA DLL 开销。观察器改为启动早期发现并缓存子进程，不在推理期间反复做全系统进程快照；64 MiB 真实子进程采样复测通过，后续 CPU 实测采样错误为空。
4. 系统资源并不独占。GPU FP32/FP16 过程采样最低主机可用 RAM 约 0.538 / 0.183 GiB；CPU 2.11 约 0.428 GiB，CPU 2.13 抽样约 1.389 GiB。CPU 2.11 进程树 RSS 采样峰值约 2.253 GiB。内存压力可能影响耗时；没有固定温度、功耗或频率，多轮空闲环境复测仍有必要。不能仅凭 Windows swap 计数确认实际分页量。
5. GPU 阶段最初的资源观察器仅采样到 venv 的小型 launcher，**其约 4 MiB RSS 不是模型进程占用，不用于 CPU/GPU RAM 比较**；主机可用 RAM 采样及 PyTorch GPU 峰值仍有效。
6. 60 条是此前调参与回归 benchmark，不是独立 holdout。候选准备采用 GPU Embedding，且计时取 top20 前 9，不保证与线上按 top9 召回的候选逐项相同。因此也不能与 Phase 12 历史 12 条纯 apple_support 计时直接拼接计算收益。
7. 没有调用真实 LLM、修改业务代码、调整系统页面文件或手动停止/重建业务容器。没有测端到端回答、并发、多用户显存峰值、GPU Docker 服务或生产 SLA。

## 验证、产物与复现

- 新增复现入口：`scripts/benchmark_reranker_devices.py`。
- 新增离线测试：`tests/test_reranker_device_benchmark.py`，100 passed；相关截断、重排 safeguard、RAG benchmark 合计 **143 passed**。GPU/CPU 隔离环境也分别跑通上述 100 项 runner 测试。
- 主 Agent 独立重算冻结 ID/qrels、排序、指标、24 次统计，并核对实际参数、设备和离线标志；4 份运行产物全部通过。
- 原始结果：`cpu_fp32_torch211.json`、`gpu_fp32.json`、`gpu_fp16.json`；版本抽样：`cpu_fp32_torch213_sample.json`。
- 输入证据：`inputs.json`、`input_validation.json`、`prep_inputs.py`、`prep_cuda.log`。
- 比较脚本：`compare_results.py`；资源观察器：`run_observed.py`。安装日志、环境包列表和资源 JSON 同目录保存。
- 结束后全局 Torch / torchvision 仍为 2.13.0+cpu / 0.28.0+cpu；8000、8001、8971 的 `/health` 均 200，smartcs-api 仍 `smartcs-api:phase11`、与 harness 均 healthy。未 commit/push。

从仓库根目录，在输入 snapshot 路径仍可用时复现（全部离线模型推理）：

```bash
../.venv-smartcs-cpu-control-20261010/Scripts/python.exe -m scripts.benchmark_reranker_devices --inputs output/gpu_rerank_eval_20261010/inputs.json --output output/gpu_rerank_eval_20261010/repeat_cpu.json --device cpu --dtype fp32 --threads 8
../.venv-smartcs-gpu-eval-20261010/Scripts/python.exe -m scripts.benchmark_reranker_devices --inputs output/gpu_rerank_eval_20261010/inputs.json --output output/gpu_rerank_eval_20261010/repeat_gpu32.json --device cuda --dtype fp32 --threads 8
../.venv-smartcs-gpu-eval-20261010/Scripts/python.exe -m scripts.benchmark_reranker_devices --inputs output/gpu_rerank_eval_20261010/inputs.json --output output/gpu_rerank_eval_20261010/repeat_gpu16.json --device cuda --dtype fp16 --threads 8
```

上述命令写入 `repeat_*.json`，不覆盖本文原始证据。`compare_results.py` 默认重核本文的四份原始 JSON，不会自动读取这些复测文件：

```bash
python output/gpu_rerank_eval_20261010/compare_results.py
```

建议：GPU FP32 已足够达到本轮 2 秒目标，可作为更稳健的迁移起点；FP16 是更快、更省显存的可选配置。部署前仍需独立查询集、显式 Embedding/reranker 设备控制、GPU 容器及真实问答端到端/并发验收，并另行获得部署授权。
