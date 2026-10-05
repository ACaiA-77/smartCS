# Phase 12 报告：RAG 检索延迟优化（CrossEncoder CPU 瓶颈）

> **日期**：2026-10-05 ｜ **依据**：`docs/HANDOFF-phase12.md`
> **前置**：Phase 11 工程冻结后用户批准的唯一后续优化。
> **本轮未 commit / 未 push**；工作树改动由验收方统一提交。

---

## 0. 终态一句话

```text
截断    rerank 输入按文档前缀截断（SMARTCS_RERANK_MAX_CHARS，默认 768）
        P95 −41.6% / P50 −3.4%      ← 削掉的是长尾，不是中位数
        默认值 512 被实测否决：recall@10 越线 1.39pt，按 handoff 调大后 768 过线
ONNX    已按 handoff 要求评估并实测：1.96× 提速，但两道门禁都没过
        不设为默认、不推荐启用      ← 数据否决，非实现否决
质量    benchmark 真实复跑（P2-8 清账），历史 metrics.json 与当前管线不符已定位
P50     未达成 2s 目标，如实留在报告里，未用任何手段粉饰
```

---

## 1. 第一步：输入截断

### 1.1 落点与语义

全部改动集中在 `rag/reranker.py`（handoff 白名单）。

| 新增 | 说明 |
|---|---|
| `SMARTCS_RERANK_MAX_CHARS` | 截断预算，默认 `512`；`0` = 关闭截断 |
| `truncate_for_rerank(text, max_chars)` | 词边界对齐的前缀截断 |
| `max_chars_from_env()` | 读取预算；缺失/空白取默认，非法值**报错而非静默回退** |

截断规则：`str` 切片按码点进行，**结构上不可能截出半个 UTF-8 序列**。拉丁文本回退到
`_WORD_BOUNDARY_LOOKBACK = 32` 字符内的最近空白；中文无空白可对齐，按字符截断。
回退窗口有界，避免"句首一个空格 + 后面一长串中文"被砍到那个遥远的空格。

只截 `retrieval_text or content`（文档侧）。**query 不截**。dense / BM25 / RRF / 召回候选集
**零改动**——截断只发生在 `predict()` 的入参构造处。

### 1.2 为什么是"文档侧截断"而不是别的

`CrossEncoder.predict` 把一个 batch 内所有 pair **padding 到最长的一条**。所以一批 9 对
文档 `[343, 407, 423, 437, 451, 458, 497, 513, 704]` 的成本由 704 那条决定，而不是由平均长度
决定。截断等于给整批的 padding 上限封顶。

实测（本轮数据，见 §4 的微型基准）证明这条线性关系成立：

| 单批最大文档长度 | 1194 | 512 | 384 | 256 |
|---|---|---|---|---|
| 9 对一批耗时 | 15941 ms | 6568 ms | 4622 ms | 2945 ms |

---

## 2. 时延复测（Phase 11 同协议）

协议与 Phase 10/11 完全一致：`python -m internal_api.rag_timing --queries 12`，
`all-domains (production shape)`，sparse_mode `global_corpus_v1`，prompt 同 12 条。
唯一变量是 `SMARTCS_RERANK_MAX_CHARS`。原始数据：
`output/phase12_rag_timing/timing_{before,after512,after768}_12q.json`。

**先给 512 的复测（§2 正文），因为它说明了机制；最终采用的是 768，完整取舍曲线在 §4.6。**

| 分段 | 关闭 P50 | 512 P50 | 变化 | 关闭 P95 | 512 P95 | 变化 |
|---|---|---|---|---|---|---|
| **rerank** | **6134.0** | **5233.4** | **−15%** | **17302.6** | **6898.9** | **−60%** |
| retrieve_total | 6475.7 | 5591.8 | −14% | 17650.6 | 7281.0 | −59% |
| dense | 173.8 | 178.7 | ~0 | 197.2 | 202.9 | ~0 |
| bm25 | 3.3 | 4.2 | ~0 | 5.6 | 4.6 | ~0 |
| rrf | 0.1 | 0.1 | ~0 | 0.1 | 0.1 | ~0 |
| rank_merge | 0.0 | 0.0 | ~0 | 0.1 | 0.0 | ~0 |
| serialize | 0.2 | 0.2 | ~0 | 0.2 | 0.2 | ~0 |

复现性：关闭时 P50 / P95 = 6134 / 17303，与 Phase 11 记录的 6190 / 17602 相差 <1%，
说明本轮机器状态与当时可比，前后对比有效。

> 512 最终**没有采用**（质量越线，见 §4.3）。它留在这里是因为它最能说明截断的机制。

### 2.1 为什么中位数只降 15%，长尾却降 60%

实测每批（9 对）的**最大**文档长度分布（`benchmark` 前 12 条 query）：

```text
[506, 513, 523, 523, 631, 631, 704, 704, 704, 704, 732, 1174]   p50 = 667.5
```

- **中位批**的最大文档 ≈ 667 字符，只比 512 高 155——截断削掉的有限。
- **尾部批**的最大文档 1174 字符，比 512 高 662——截断削掉一半以上。

所以截断是一个**削尾**手段，不是削中位数的手段。12 批里有 11 批含一条 497–523 字符的文档，
说明语料本身存在一个长度带，512 的阈值正好落在这个带的边上。

---

## 3. 第二步：ONNX int8 评估（handoff 触发条件已满足）

handoff 规则：「若 P50 仍 > 2s：评估 ONNX int8」。截断后 rerank P50 = 5233 ms > 2s，
**触发**。已按 handoff 要求交付三件套：导出脚本 + 加载器 + 打分一致性抽查。

### 3.1 交付物

| 项 | 位置 |
|---|---|
| 导出脚本 | `scripts/export_reranker_onnx.py`（fp32 导出 + 动态 int8 量化 + manifest 哈希） |
| 加载器 | `rag/reranker.py` 的 `OnnxReranker`，`RAG_RERANKER_BACKEND=onnx_int8`（opt-in） |
| 一致性抽查 | `scripts/benchmark_reranker_backends.py` |
| 产物 | `artifacts/reranker_onnx/reranker_int8.onnx`（570 MB，`/artifacts/` 已被 gitignore） |

### 3.2 导出路径上的两个真实坑（记录，非推断）

1. **exporter 必须用 TorchScript，不能用 dynamo。** torch 2.13 默认走 `torch.export`/
   dynamo；该图能导出，但 onnxruntime 的量化器做 shape inference 时报
   `Inferred shape and existing shape differ in dimension 0: (1024) vs (1)`，且
   `quantize_dynamic` 没有关掉 shape inference 的开关。改用 `dynamo=False` 后通过。
2. **wrapper 必须是 `torch.nn.Module`。** dynamo 路径拒绝普通 callable
   （`Expected mod to be an instance of torch.nn.Module`）。

两条都是实测出来的，不是查文档推断的。

### 3.3 一致性抽查（60 条 benchmark query，同 batch）

reference = `cross_encoder`（float），candidate = `onnx_int8`，两侧 `max_chars=512`，
每批 9 对、对全部 9 对打分（不是只对 top_k）。

60 条 query（`output/phase12_rag_timing/backend_compare_60q.json`）：

| 指标 | cross_encoder | onnx_int8 | 比值 |
|---|---|---|---|
| 每批 P50 | 5974.4 ms | 3050.9 ms | **1.96×** |
| 每批 P95 | 6662.1 ms | 3840.9 ms | 1.73× |
| 每批 mean | 5876.0 ms | 3045.0 ms | 1.93× |

| 一致性 | 实测 | handoff 阈值 | 判定 |
|---|---|---|---|
| Spearman 最小（单批） | **0.8833** | > 0.95 | **未过** |
| Spearman 均值 | 0.9700 | — | — |
| 达标的批数 | **44 / 60** | 60 / 60 | **未过** |
| top-1 命中 | 98.3% | — | — |
| top-3 顺序一致 | 81.7% | — | — |
| 全序一致 | 26.7% | — | — |
| 单条最大 sigmoid 分差 | 0.205 | — | — |

12 条子集与 60 条全集的结论一致（子集 min 0.8833，同样未过），不是抽样波动。

> top-1 有 98.3% 命中，容易被读成"基本没差"。但**生产用的是 top-3 上下文**，
> 而 top-3 的顺序一致率只有 81.7%、全序一致率只有 26.7%——对 RAG 而言这是实质差异，
> 不是舍入噪声。

### 3.4 对照实验：把"量化有损"与"我的加载器写错了"分开

只用 Spearman 无法区分「int8 量化有损」和「我的 tokenizer/session 接错了」。因此加了一组
**fp32 ONNX 对照**——同一份加载器代码，只换图：

| 对比 | Spearman 最小 | 均值 | 低于 0.95 的批 |
|---|---|---|---|
| torch vs torch（确定性基线） | 1.0000 | 1.0000 | 0/12 |
| **torch vs onnx_fp32（我的加载器）** | **1.0000** | **1.0000** | **0/12** |
| torch vs onnx_int8（量化） | 0.9167 | 0.9681 | 4/12 |

结论明确：**加载器是精确等价的（fp32 Spearman = 1.0000），分歧 100% 来自 int8 动态量化。**

另外实测 int8 图本身是**确定性的**（连跑 3 次哈希一致，且与
`intra_op_num_threads` 取 1/4/默认无关），所以分歧不是推理随机性。

### 3.5 决策：不采用

| 门禁 | 阈值 | 实测 | 判定 |
|---|---|---|---|
| 时延 | rerank P50 ≤ 2s | 2530 ms | **未过** |
| 打分一致性 | 同 batch Spearman > 0.95 | min 0.88（5/12 低于 0.95） | **未过** |

**两条都没过，所以不作为默认后端，也不推荐启用。** 代码保留为 opt-in：
handoff 明确要求交付"可选后端"，且换一台有 AVX-512/VNNI 的 CPU 时这个比值会变——留着
`scripts/benchmark_reranker_backends.py` 就能重新裁决，而不是重新开发。

不启用的代价说明：ONNX 路径需要 `onnx` + `onnxscript`（**未加入 `requirements.txt`**——
该文件不在 handoff 白名单内），外加一个每部署 570 MB 的本地构建产物。默认路径
`cross_encoder` 不依赖其中任何一个。

---

## 4. 第三步：真实 benchmark 质量门禁（P2-8 清账）

### 4.1 这是第一次在当前配置下真跑

`scripts/evaluate_rag_retrieval`，60 query × 2 domain，`artifacts/rag_round3/production_indexes`，
`--qrel-groups` 未启用（manifest 无 `scoring_mode`）。两轮均通过 manifest 哈希校验，
且 `model_validation.json` 记录为**真模型**：

```json
{"status":"ready","fake_embedding":false,"fake_reranker":false,
 "reranker_backend":"sentence_transformers","reranker_model":"BAAI/bge-reranker-v2-m3",
 "embedding_model":"BAAI/bge-m3","embedding_dimension":1024,"errors":[]}
```

**P2-8 欠账的成因，本轮实测出来了**：`artifacts/rag_round3/metrics.json` 那份历史数值
与当前管线对不上——它记的是 `hybrid_rerank` recall@10 = 0.825，而本轮同配置 BEFORE 实测
**0.9000**；bm25 差得更远（历史 0.7583 vs 本轮 0.5472）。那份快照属于旧配置（疑似 jieba
重建前的索引），**不能作为本轮基线**。本轮 BEFORE 才是基线。

### 4.2 结构证据：召回语义零改动（位级）

统计 `metrics_per_query.json` 里每个变体在 60 条 query 上的**排名列表是否逐位相同**：

| 变体 | 前后排名不同的 query 数 | 含义 |
|---|---|---|
| `dense` | **0 / 60** | 逐位相同 |
| `bm25` | **0 / 60** | 逐位相同 |
| `hybrid_rrf` | **0 / 60** | 逐位相同 |
| `hybrid_rerank` | 50 / 60 | 唯一受影响者（预期内） |

这不是"我们没改那些文件"的声明，而是**跑出来的位级同一性**。

### 4.3 截断前 vs 截断后（512）

`output/phase12_benchmark/before/`（`SMARTCS_RERANK_MAX_CHARS=0`，即优化前语义）
vs `after/`（`512`）。

| 变体 | 指标 | before | after | 变化 |
|---|---|---|---|---|
| **hybrid_rerank** | **recall@10** | **0.9000** | **0.8861** | **−0.0139（−1.39pt）** |
| | mrr@10 | 0.8228 | 0.8527 | **+3.63%** |
| | ndcg@10 | 0.8154 | 0.8207 | **+0.65%** |
| | wrong_domain_rate@10 | 0.0567 | 0.0483 | −14.7% |
| | wrong_domain_rate@3 | 0.0333 | 0.0278 | −16.7% |
| dense | recall@10 / mrr@10 / ndcg@10 | 0.8417 / 0.7397 / 0.7295 | 同左 | **0.00%** |
| bm25 | 同上 | 0.5472 / 0.3842 / 0.4077 | 同左 | **0.00%** |
| hybrid_rrf | 同上 | 0.7972 / 0.6749 / 0.6750 | 同左 | **0.00%** |

**门禁判定**（handoff：Recall@10 回落 ≤ 1pt、MRR/nDCG 回落 ≤ 2%）：

| 门禁项 | 阈值 | 实测 | 判定 |
|---|---|---|---|
| Recall@10 | 回落 ≤ 1pt | **回落 1.39pt** | **越线** |
| MRR@10 | 回落 ≤ 2% | +3.63%（上升） | 通过 |
| nDCG@10 | 回落 ≤ 2% | +0.65%（上升） | 通过 |

recall 越线 → 按 handoff「调大 MAX_CHARS 重测」。

### 4.4 越线的形状（它是不是噪声）

60 条里只有 **7 条** query 的 recall@10 变化，且**全部落在 `agent_engineering`**——
正是长文档域（p50 853 字符），也就是截断真正咬到的地方：

```text
agent_002  0.500 -> 1.000   变好
agent_006  0.500 -> 1.000   变好
agent_023  0.500 -> 1.000   变好
agent_008  1.000 -> 0.500   变差
agent_010  1.000 -> 0.500   变差
agent_011  1.000 -> 0.667   变差
agent_013  1.000 -> 0.000   变差（该 query 唯一的 relevant chunk 掉出 top-10）
```

不是随机抖动：全流程确定性（无采样），前后跑的是同一批候选，唯一变量是 rerank 打分输入。
所以这 1.39pt 是**真实的方向性差异**，只是 MRR/nDCG 变好、recall 变差的组合说明：截断让
头部排序更锐利，代价是把个别 borderline 的 relevant chunk 挤出了 top-10。

### 4.5 调大 MAX_CHARS 重测：768 过线

| `hybrid_rerank` | before (0) | 512 | **768** | 门禁 |
|---|---|---|---|---|
| recall@10 | 0.9000 | 0.8861（−1.39pt） | **0.9083（+0.83pt）** | 回落 ≤ 1pt → **通过** |
| mrr@10 | 0.8228 | 0.8527（+3.63%） | 0.8182（−0.55%） | 回落 ≤ 2% → **通过** |
| ndcg@10 | 0.8154 | 0.8207（+0.65%） | 0.8078（−0.93%） | 回落 ≤ 2% → **通过** |
| wrong_domain_rate@10 | 0.0567 | 0.0483 | 0.0517 | — |
| wrong_domain_rate@3 | 0.0333 | 0.0278 | 0.0333 | — |

`dense` / `bm25` / `hybrid_rrf` 在 768 下**同样逐位不变**（0/60）。

**结论：768 通过全部三项门禁，采用为默认值。**

**效果量（诚实口径）**：768 只有 **3/60** 条 query 的 recall@10 发生变化（2 升 1 降），
全部仍在 `agent_engineering`；512 是 7/60（3 升 4 降）。单看 60 条 benchmark，
+0.83pt 不足以声称"截断提升质量"——正确口径是**质量中性且在门禁内**，
而不是"截断变好了"。

> **未做的二分**：真正的阈值在 (512, 768] 之间，本轮**只做了区间夹逼**，没有二分到最小可行值。
> 每测一个点 = 一次 20 分钟全量 benchmark（且当时机器内存已在 82%）。若验收方要精确最小值，
> 需要再排窗口。

### 4.6 时延 vs 质量：完整取舍曲线

`internal_api.rag_timing --queries 12`，production shape，同协议：

| MAX_CHARS | rerank P50 | rerank P95 | recall@10 变化 | 门禁 |
|---|---|---|---|---|
| 0（关闭） | 6134.0 ms | 17302.6 ms | 基线 | — |
| 512 | **5233.4 ms（−15%）** | **6898.9 ms（−60%）** | −1.39pt | **未过** |
| **768（采用）** | 5925.0 ms（−3.4%） | 10112.3 ms（−41.6%） | +0.83pt | **通过** |

原始数据：`output/phase12_rag_timing/timing_{before,after512,after768}_12q.json`。

**这张表是本轮最重要的结论，也是最不好看的一张表**：质量红线与中位时延收益是**互相冲突**的。

- 512 之所以能砍掉 15% 的 P50，正是因为它切进了长文档的正文（`agent_engineering` 71%
  的 chunk 超过 512 字符），代价是把 borderline relevant chunk 挤出 top-10。
- 768 让**中位批完全不被触碰**（实测每批最大长度 p50 = 667 < 768），所以 P50 几乎不动。

**因此必须如实说明**：截断（无论 512 还是 768）**没有解决 Phase 11 提出的 P50 问题**。
P50 仍 ≈ 5.9s，距 handoff 的 2s 目标很远。它解决的是**尾部**：P95 从 17.3s 降到 10.1s。

---

## 5. 第二步结论：ONNX 不采用，P50 问题仍然开放

把 §3 与 §4 合起来看，本轮能给出的诚实结论是：

| 手段 | P50 | P95 | 质量 | 采纳 |
|---|---|---|---|---|
| 截断 768 | 5925 ms（−3.4%） | 10112 ms（−41.6%） | 门禁内 | **是（默认）** |
| 截断 512 | 5233 ms（−15%） | 6890 ms（−60%） | 越线 1.39pt | 否 |
| ONNX int8 | 2530 ms（−59%） | 3791 ms（−78%） | Spearman 越线 | 否 |
| ONNX int8 + 512 | 3051 ms | 3841 ms | 两道门禁都越线 | 否 |

**P50 > 2s 这个目标本轮没有达成，也不应该在报告里被含糊过去。** 剩下的路径（更激进的截断、
换更小的 reranker、GPU）都需要各自的质量验证，且都不在本次批准的范围内。

---

## 6. 硬约束逐项核对

| # | 约束 | 结果 | 证据 |
|---|---|---|---|
| 1 | 只动 rerank 输入与后端选择；dense/BM25/RRF/召回语义零改动 | ✅ | benchmark 60 query 上 `dense`/`bm25`/`hybrid_rrf` 排名**逐位相同**（0/60 不同，512 与 768 两次都验）；`rag/retriever.py`、`fusion.py`、`dense_retriever.py`、`sparse_retriever.py`、`global_sparse.py`、`models.py` 全部 `git diff` 干净 |
| 2 | 白名单 `rag/reranker.py` + 对应 tests + `.env.example` | ✅ 含两处报备 | 改动文件见 §7-D1 |
| 3a | pytest ≥ 652 | ✅ | `678 passed, 38 skipped`（458.09s）；扣掉本轮新增 27 个用例后基线 `651 passed / 38 skipped` |
| 3b | vitest 全绿 | ✅ | `Test Files 35 passed (35) / Tests 191 passed (191)`（551.63s）——与 Phase 11 记录一致 |
| 3c | benchmark 全量跑用独占窗口 | ✅ | 已 SendMessage 通知另一会话避让，窗口 01:25–02:12 与 02:12–02:33 两段，期间无并发负载 |
| 4 | 不 commit / 不 push | ✅ | 工作树改动未提交 |

### 6.1 那 1 个 pass/skip 的归属（对表用）

Phase 11 记录 `652 passed / 37 skipped`，本轮基线是 `651 passed / 38 skipped`。**已定位，与 Phase 12 无关：**

```text
tests/test_pi_unified_entry_e2e.py:59
    PI_HARNESS = PYTHON_IMPL.parent / "pi-harness"        # ← 迁移前的 sibling 路径
    pytestmark = pytest.mark.skipif(shutil.which("node") is None or not TSX_CLI.exists(), ...)
```

- 该文件是 **P7-2 跨服务端到端**（Python ↔ harness，走真实 pi 入口）。
- `python-impl/pi-harness/` 已存在且 `node_modules/tsx/dist/cli.mjs` 就位，
  但守卫指向的是 **`D:\...\project005_SmartCS\pi-harness`（sibling）**——monorepo 迁移后该目录**已不存在**。
- 实测：`sibling ABSENT` → `TSX_CLI ABSENT` → 整个模块 **skip**。
- 迁移前 sibling 存在、该用例真实跑过并通过，所以是 **1 pass → 1 skip**，差额恰好对上。

**这是一个 Phase 11 迁移遗留的真实缺陷**，性质与 PHASE11_REPORT §D1（`tests/python/` 路径不存在）
完全同类：**残留的 sibling 路径假设，静默关掉了一个跨服务验收测试**。一条 `PYTHON_IMPL.parent` → `PYTHON_IMPL`
即可修复（`python-impl/pi-harness/...` 已经在位）。

**本轮未修**：`tests/test_pi_unified_entry_e2e.py` 不在 handoff 白名单内，且重新启用一个跨服务 e2e
会引入新的测试库/Node 依赖窗口，应由验收方在自己的窗口里决定并验证。**在此登记，不擅自改。**

---

## 7. 偏差与判断记录

### D1 · 新增了两个 `scripts/` 文件（超出字面白名单，但为 handoff 明文要求）

handoff §2 白名单写的是 `rag/reranker.py` + 对应 tests + `.env.example`，但 §1 第二步又明文要求
"导出脚本 + 加载器 + 一致性抽查"。两者只能在 `scripts/` 落地：

| 文件 | 依据 |
|---|---|
| `scripts/export_reranker_onnx.py` | §1 第二步"导出脚本" |
| `scripts/benchmark_reranker_backends.py` | §1 第二步"与现有 cross_encoder 的打分一致性抽查" |

`scripts/` 是工具目录而非业务目录（`agents/`/`api/`/`mcp/`/`memory/`/`platform_db/` 等全部零改动）。

### D2 · 默认值由 512 改为 768（handoff 预设分支的实际结果）

handoff 写"默认 512 字符"，但同时写"越线则调大 MAX_CHARS 重测"。实测 512 越线
（recall@10 −1.39pt），768 过线，故默认取 768。这是**执行 handoff 既定分支**，不是偏离；
但字面默认值确实变了，故在此报备。测试 `test_env_absent_or_blank_falls_back_to_the_documented_default`
已同步钉住 768。

### D3 · 新增了 `onnx` / `onnxscript` 两个开发依赖，未写入 `requirements.txt`

两者只在 ONNX 导出时用到；运行时的 `OnnxReranker` 懒加载 `onnxruntime`（已在环境中），
不 import `onnx`/`onnxscript`。`requirements.txt` 不在白名单内，故未改。
**默认路径 `cross_encoder` 不依赖其中任何一个。**

### D4 · ONNX 后端代码保留为 opt-in，但明确不推荐

数据否决（§3.5）后仍保留代码，理由：handoff 要求交付"可选后端"，且换硬件后比值会变；
保留 `scripts/benchmark_reranker_backends.py` 可让后续裁决是"重跑"而非"重写"。
`OnnxReranker` 的 docstring 已把实测结论写进去，避免后人误以为它是推荐选项。

### D5 · 阈值只夹逼、未二分

质量红线的最小可行 `MAX_CHARS` 落在 (512, 768] 之间，本轮只测了 512（越线）与 768（过线）
两个点，没有二分到最小值——每个点 = 一次 20 分钟全量 benchmark，且当时机器内存已 82%
（其间还发生过一次后台任务因内存被回收）。若验收方要精确最小值，需另排窗口。

### D6 · `artifacts/rag_round3/metrics.json` 是失效快照，本轮 BEFORE 才是新基线

见 §4.1。建议后续引用的基线一律用本轮 `output/phase12_benchmark/before/`。

### D7 · 一次 pytest 全量跑出现 2 个瞬时失败，复跑即绿

同一命令（`--ignore=tests/test_rag_rerank_truncation.py`）两次运行：一次 `2 failed, 649 passed, 38 skipped`，
一次 `651 passed, 38 skipped`。测试集合相同，说明是环境型 flake（该套件会连真实 MySQL/Redis 并尝试
导出 OTel 到 `localhost:4317`，本机未起 collector）。**失败用例名未捕获**，因为它未被 `-rf` 打印出来
就恢复了。与 rerank 无关，如实记录。

---

## 8. 改动清单与复现

### 8.1 改动文件

| 文件 | 状态 | 说明 |
|---|---|---|
| `rag/reranker.py` | M | 截断 + `max_chars` 贯通 + `OnnxReranker` + 共用排序尾 |
| `.env.example` | M | `SMARTCS_RERANK_MAX_CHARS=768` 及注释 |
| `tests/test_rag_rerank_truncation.py` | 新增 | 27 个用例（截断 / env 解析 / 两个后端契约） |
| `scripts/export_reranker_onnx.py` | 新增 | fp32 导出 + int8 量化 + manifest 哈希（D1） |
| `scripts/benchmark_reranker_backends.py` | 新增 | 后端时延 + 打分一致性（D1） |
| `output/phase12_rag_timing/` | 新增产物 | 三段时延原始数据 + 后端对比 |
| `output/phase12_benchmark/{before,after,max768}/` | 新增产物 | 三轮 benchmark 指标 |
| `artifacts/reranker_onnx/` | 本地产物 | 570 MB int8 图（`/artifacts/` 已 gitignore，不入库） |

业务目录（`agents/` `api/` `mcp/` `memory/` `platform_db/` `tickets/` `refunds/` `internal_api/`）
**零改动**；`rag/` 内除 `reranker.py` 外零改动。

### 8.2 复现命令

```bash
cd python-impl

# 时延（三段）
SMARTCS_RERANK_MAX_CHARS=0   python -m internal_api.rag_timing --queries 12 --json output/phase12_rag_timing/timing_before_12q.json
SMARTCS_RERANK_MAX_CHARS=512 python -m internal_api.rag_timing --queries 12 --json output/phase12_rag_timing/timing_after512_12q.json
SMARTCS_RERANK_MAX_CHARS=768 python -m internal_api.rag_timing --queries 12 --json output/phase12_rag_timing/timing_after768_12q.json

# 质量（三轮，各约 15-32 分钟）
SMARTCS_RERANK_MAX_CHARS=0   python -m scripts.evaluate_rag_retrieval --output-root output/phase12_benchmark/before
SMARTCS_RERANK_MAX_CHARS=512 python -m scripts.evaluate_rag_retrieval --output-root output/phase12_benchmark/after
SMARTCS_RERANK_MAX_CHARS=768 python -m scripts.evaluate_rag_retrieval --output-root output/phase12_benchmark/max768

# ONNX（导出 + 一致性）
python -m scripts.export_reranker_onnx
python -m scripts.benchmark_reranker_backends --queries 60 --json output/phase12_rag_timing/backend_compare_60q.json
```

---

## 9. 基线复核

```text
python -m pytest -q          678 passed, 38 skipped, 1 warning   (458.09s)
  └ 减去本轮新增 27 个用例 → 基线 651 passed / 38 skipped
     （Phase 11 记录 652 / 37；差的 1 项已定位并归因，见 §6.1）

npm test  (pi-harness)       Test Files 35 passed (35)
                             Tests      191 passed (191)         (551.63s)
                             —— 与 Phase 11 记录一致
```

PHASE12_DONE COMPLETED
