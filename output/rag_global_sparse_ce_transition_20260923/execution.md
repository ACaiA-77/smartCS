# Dev v4 CE 转移诊断执行记录

- TASK_ID: `smartcs_global_sparse_ce_transition_diagnosis_20260923`
- 范围：只用 Dev v4 保存的 A（domain-local BM25）和 B（global sparse）结果分类；重放有损失的 5 题的 RRF20→CE20。没有读取或运行 Holdout v1，没有改 CE、Top20/Top10、阈值、权重、safeguard 或默认模式。
- 用已保存的 60 题指标先选出：RRF 任一 @10 指标提高但 CE 任一指标下降的 `apple_010/011/027`；另有 CE 下降的 `apple_002`、`agent_007`。前者三题没有 CE Recall 下降；全体最终 CE Recall 的净损失由 `apple_002` 造成。
- `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -m output.rag_global_sparse_ce_transition_20260923.analyze` 最终退出码 0。使用本地真实 BGE 与 CE；5/5 题 A/B Dense、BM25、RRF10、CE10 与各自保存的 Dev 结果逐题一致。先尝试对 60 题全部重放，1 题后停止以节省模型调用；随后只补充 3 个交集题及另外 2 个 CE 损失题，结果保存在同一 `traces.jsonl`。
- 相同 query/chunk pair 的 A/B CE score 最大绝对差约 7.9e-7，支持“分数稳定、相对竞争变了”。三个交集题的相关项仍在 CE10，只是排序下降。
- `apple_002` 的 grade-1 qrel 在 A RRF4/CE10，B RRF6/CE11；B-only 同域候选在 B CE10（0.1939），高于该 qrel 的 0.1600。候选集竞争与 RRF 位次后移同时存在，不能把损失单独归因于 CE。`agent_007` 相关项从 CE6→CE7，nDCG 小幅下降。
- 证据：`artifacts/rag_global_sparse_ce_transition_20260923/{summary.json,summary.md,mechanism.md,traces.jsonl}`；`mechanism.md` 列出 qrel grade、A/B RRF/CE 位次与分数、A-only/B-only RRF20 候选的来源和得分。未列入 qrels 的候选只称“未标注”，不视为已证非相关。
- 这是 seen Dev 机制诊断，不是修复效果或泛化验证；任何 CE/fusion 策略调整前应先冻结独立选择集。
- 检查：`python -m output.rag_global_sparse_ce_transition_20260923.select` → 60/60 源题、qrels、Dense 排序一致，筛出的 5 题与预期一致；`python -m pytest tests/test_rag_global_sparse.py tests/test_rag_benchmark.py -q` → 9 passed；`git diff --check` → 退出码 0（仅 Git LF/CRLF 提示）。
