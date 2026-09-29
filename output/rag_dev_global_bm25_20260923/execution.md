# Dev v4 统一语料 BM25 反事实执行记录

- TASK_ID: `smartcs_dev_global_bm25_counterfactual_20260923`
- 范围：只读 Dev v4 与冻结生产 BM25 postings；没有读取或运行 Holdout v1，没有修改索引、production retrieval、Dense/RRF/CE、qrels、k1/b。
- A：现有 domain-local BM25 各取 Top20，再按 raw score 合并 Top10。60 条 Dev 排序及对应指标逐条与保存的 corrected baseline 一致；baseline 的 query/domain/kind/qrels 又逐条与当前 Dev benchmark 一致。
- B：复用两个冻结 BM25 index 的 `postings`、`document_lengths` 与 `document_frequency`，合并 N、df、avgdl，在同一 60 条 Dev query 上计算统一语料 BM25。没有重新 chunk、分词或建索引；query 使用当前运行时 `_terms`，其历史构建 backend 不另作推断。
- 预注册规则写入运行前的脚本：Apple wrong-domain@10 绝对下降至少 0.10，wrong-domain@1 不增加，全 Dev Recall/MRR/nDCG@10 各下降不超过 0.02；没有 sweep 或选择 k1/b。
- 结果：Apple wrong-domain@10 0.4633→0.0867，@1 0.1667→0；Apple Recall/MRR/nDCG@10 分别 +0.0889/+0.1111/+0.0999；全 Dev 分别 +0.0444/+0.0556/+0.0493。达到预注册支持条件，只支持该机制解释，并非唯一因果证明或跨集泛化结论。
- 命令：`python -m output.rag_dev_global_bm25_20260923.analyze`，退出码 0；补充 benchmark 源一致性断言后再次运行，退出码 0、结果不变。`python -m pytest tests/test_rag_benchmark.py -q` → 8 passed；`git diff --check` → 退出码 0（只有 Git LF/CRLF 提示）。
- 输出：`artifacts/rag_dev_global_bm25_20260923/counterfactual.json`（逐题排序、分数、输入 SHA-256、判定规则）、`counterfactual.md`（汇总）。
