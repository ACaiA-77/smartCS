# Round 3 retrieval failure analysis

Real BGE-M3 and BGE Cross-Encoder models loaded offline and the production benchmark completed.

- Dense-only, BM25-only, Hybrid RRF, and Hybrid + rerank metrics are in `metrics.json`.
- Cross-domain contamination is reported as `wrong_domain_rate@1/3/5/10`.
- Query authoring and qrel audit evidence is in `query_overlap_report.json`.

## Observed benchmark cases

- Dense succeeds while BM25 misses: apple_011 [apple_support] 如果突然想起密码，已经提交的恢复请求怎样处理？ — dense recall@10=1.000, mrr@10=0.100
- BM25 succeeds while Dense misses: apple_030 [apple_support] 我能否从知识库直接查询具体订单退款金额，还是应转到官方实时页面？ — bm25 recall@10=1.000, mrr@10=1.000
- Reranker improves recall: apple_010 [apple_support] Apple 账户恢复等待多久，联系客服能否提前结束？ — hybrid_rerank recall@10=1.000, mrr@10=1.000
- Reranker regresses recall: apple_021 [apple_support] 中国官网购买的商品不满意，通常要在多久内申请退货？ — hybrid_rerank recall@10=0.500, mrr@10=1.000
- Cross-domain contamination: apple_001 [apple_support] Apple ID 改名后，原来的邮箱和密码还能登录吗？ — hybrid_rerank recall@10=1.000, mrr@10=1.000
