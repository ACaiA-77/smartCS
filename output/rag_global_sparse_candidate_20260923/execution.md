# Global sparse 工程候选执行记录

- TASK_ID: `smartcs_global_sparse_candidate_20260923`
- 模式：新增 `global_corpus_v1` 显式候选；`HybridRetriever` 默认仍为 `domain_local_v1`。多域走全局稀疏索引，单域仍走原 BM25；Dense、RRF k=60、CE、Top20/Top10 未改。
- 构建：`python -m scripts.build_global_sparse --artifact-root artifacts/rag_round3/production_indexes`，退出码 0。新增 `global_sparse/{bm25_index.json,manifest.json}`；1475 个 chunk，k1=1.5、b=0.75。manifest 绑定来源 manifest/chunks/BM25 哈希并注明 tokenizer 构建来源不明。重复构建通过同字节检查；正式加载校验来源哈希、chunk 覆盖、长度、df 与 postings/TF 一致性。
- 纯工程 parity：`python -m output.rag_global_sparse_candidate_20260923.verify_parity`，退出码 0；Dev 60 题候选 BM25 Top10 排序全部等于前轮离线反事实 B，分数差不超过 1e-10。
- Dev 完整链路：`HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -m output.rag_global_sparse_candidate_20260923.evaluate_dev`，退出码 0。BAAI/bge-m3 与 BAAI/bge-reranker-v2-m3 本地真实模型验证 ready，fake flags false；60/60 Dense 排序与当前 Dev baseline 一致，60/60 BM25 排序与离线 B 一致。
- Dev @10，B−A：BM25 wrong-domain -0.1883、Recall +0.0444、MRR +0.0556、nDCG +0.0493；RRF wrong-domain -0.0667、Recall +0.0417、MRR +0.0111、nDCG +0.0170；最终 CE wrong-domain -0.0117，但 Recall -0.0083、MRR -0.0083、nDCG -0.0080。Apple 的最终 CE Recall 0.9667→0.9500；Agent 最终 CE Recall 不变。
- 判定：候选稀疏路径与离线计算一致，RRF 层有收益；最终 CE 指标出现小幅退化，不能据此将候选切成默认。Dev v4 已用于机制选择，不是 unseen 验证；未读取或重跑 Holdout v1，未调 k1/b、权重或 reranker。
- 证据：`artifacts/rag_global_sparse_candidate_dev_20260923/{model_validation.json,metrics_per_query.json,metrics.json,metrics_by_domain.json,comparison.json}`，另有逐题实时记录 `metrics_per_query.jsonl`。
- 检查：`python -m pytest tests/test_rag_global_sparse.py tests/test_rag_round2.py -q` → 13 passed；`python -m pytest -q` → 409 passed、18 skipped、退出码 0（可选 OTel 4317 exporter 连接警告）；`git diff --check` → 退出码 0（Git LF/CRLF 提示）。
