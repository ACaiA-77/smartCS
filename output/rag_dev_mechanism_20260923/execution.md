# Dev v4 检索机制诊断执行记录

- TASK_ID: `smartcs_dev_retrieval_hypothesis_validation_20260923`
- 范围：诊断分析仅使用 Dev v4，没有读取 Holdout v1 数据或结果；另做了一次冻结 Gold 完整性校验。没有重跑 Holdout，也没有改动检索实现、参数、索引或 qrels。
- 输入：`artifacts/rag_corrected_baseline_20260921/metrics_per_query.json` 和 `artifacts/rag_stage_diagnosis_20260921/candidate_evidence.jsonl`；SHA-256 记录在 `artifacts/rag_dev_mechanism_20260923/diagnosis.json`。
- 命令：`python -m output.rag_dev_mechanism_20260923.analyze`，退出码 0；30 条 Apple BM25 Top10 排序逐条与冻结 Dev baseline 一致，30 条 Agent trace 与 baseline 排序一致。
- Apple：运行时 `_terms` 使用 `fallback_cjk`，30 题 query token 单字比例 0.850；BM25 Top10 跨域 139/300；Agent local Top1 高于 Apple local Top1 为 5/30。领域内 IDF 与原始分数跨域合并是代码事实；它是否单独造成误召，尚无反事实证明。索引构建时的 tokenizer backend 未独立证实。
- Agent：`agent_002`、`agent_006`、`agent_021`、`agent_023` 共 4 个相关 qrel 在 RRF Top20 中，CE 后排在 11–14 位。它们都是 grade 1；`diagnosis.json` 保留分数、位置、来源及邻近非相关候选。关于 CE 语义偏好、同源竞争或截断边界的描述仅是假设。
- Dev 已覆盖两种待诊断形态，本阶段未建立 challenge set。
- 检查：`python -m pytest tests/test_rag_stage_diagnosis.py tests/test_rag_benchmark.py -q` → 19 passed；`python -m scripts.freeze_rag_holdout check` → PASS；`git diff --check` → 退出码 0（仅有 Git LF/CRLF 提示）。
- 输出：`artifacts/rag_dev_mechanism_20260923/diagnosis.json`、`diagnosis.md`。仓库原有 Dev WIP 保持未提交状态；本阶段未触碰其文件。
