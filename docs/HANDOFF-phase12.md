# 交接指令：Phase 12 — RAG 检索延迟优化（CrossEncoder CPU 瓶颈）

> **发件方**：Claude Code（规划与验收方） ｜ **日期**：2026-10-05
> **前置**：Phase 11 工程冻结后用户批准的唯一后续优化。瓶颈数据（Phase 11 实测）：**rerank = 95% 时延**（P50 6190ms / P95 17602ms；每对 122-1653ms 随文档长度线性；候选 9 对）；模型每进程仅加载一次、候选量不大——**真凶就是 CPU 上 CrossEncoder 的逐对推理**。

## 1. 策略：便宜先行、分析驱动、质量红线

### 第一步：输入截断（预期主收益）
- CrossEncoder 输入按文档前缀截断：新增 `SMARTCS_RERANK_MAX_CHARS`（默认 512 字符，可配），截断点做词边界对齐（中文按字符、勿截半个 UTF-8 序列）。
- 依据：时延线性于输入长度——P95 的 1653ms/对来自长文档，截断直接砍掉线性段的尾部。
- 截断只影响 rerank 打分输入，**不动 dense/BM25 检索与召回候选集**。

### 第二步：测量后决策（分析先行纪律）
- 截断落地后用 Phase 11 的分段计时复测 P50/P95。
- 若 P50 仍 > 2s：评估 ONNX int8 量化 reranker 作为**可选后端**（`RAG_RERANKER_BACKEND=onnx_int8`，opt-in）：导出脚本 + 加载器 + 与现有 cross_encoder 的打分一致性抽查（同 batch Spearman 相关 > 0.95）。若 P50 ≤ 2s：ONNX 不做，报告说明。
- **不做 GPU**（本机 CPU-only）、不减少候选数（9 对已是最小充分量）、不降 top_k。

### 第三步：质量回归门禁（同时补上 P2-8 陈年欠账）
- **跑真实 benchmark**（既有 `scripts/evaluate_rag_retrieval` 基础设施）：优化前后各一轮，指标 Recall@10 / MRR@10 / nDCG@10 / wrong-domain。
- **门禁**：Recall@10 回落 ≤ 1pt、MRR/nDCG 回落 ≤ 2%；越线则调大 MAX_CHARS 重测，仍越线则回退截断默认值并如实报告。
- 这是 P2-8"benchmark 数值复跑"遗留项的正式清账——**数值必须真实跑出，不许结构性推断**。

## 2. 硬约束

1. 只动 rerank 输入与（如触发）reranker 后端选择；dense/BM25/RRF/召回语义零改动。
2. python-impl 白名单：`rag/reranker.py`（或等价落点）+ 对应 tests + `.env.example` 注释。其余业务目录零改动。
3. 基线纪律：pytest ≥ 652、vitest 全绿（测试库窗口声明）；benchmark 全量跑用独占窗口（CPU 满载约 30-90 分钟，期间通知我避让）。
4. 不 commit / 不 push；报告 `pi-harness/PHASE12_REPORT.md`（前后延迟表 + 质量对照表 + ONNX 决策依据）；完成 SendMessage 通知我；终行行首 `PHASE12_DONE <状态词>`（正文勿引用标记）。
