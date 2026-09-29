"""Print a compact, read-only evidence worklist for the proposed 36 questions."""
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
root = Path(__file__).resolve().parents[2]
out = root / "artifacts/rag_holdout_candidates_20260921"
index = root / "artifacts/rag_round3/production_indexes"


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


chosen = {
    "apple": [4, 6, 9, 10, 12, 13, 15, 17, 21, 23, 25, 28, 29, 32, 33, 36, 39, 40],
    "agent": [2, 3, 4, 5, 10, 14, 15, 16, 17, 22, 23, 25, 29, 31, 33, 35, 39, 40],
}
selected = {f"holdout_{domain}_{number:03d}" for domain, nums in chosen.items() for number in nums}
all_reviews = rows(out / "candidate_review.jsonl")
reviews = [r for r in all_reviews if r["query_id"] in selected]
leakage = {r["query_id"]: r for r in json.loads((out / "leakage_report.json").read_text(encoding="utf-8"))["queries"]}
corpus = {r["chunk_id"]: r for domain in ("apple_support", "agent_engineering") for r in rows(index / domain / "chunks.jsonl")}
assert len(selected) == len(reviews) == 36

# Additional relevance judgments after reading the original frozen chunk bodies.
# Grade 1 supports only part of the question; grade 2 answers it directly.
extra = {
    "holdout_apple_004": {"5fa027f543c0c06bd2695405": (2, "退款帮助中的收据搜索步骤也直接识别购买项目所用 Apple 账户。")},
    "holdout_apple_023": {"c75a7618833b7ea784055caf": (1, "截断的相邻片段保留无使用痕迹和外部检测要求，但未保留完整原包装条件。")},
    "holdout_agent_002": {"570d9be3ec1f52e5ff58c4d8": (1, "说明缓存要求请求前缀稳定，但未直接说恢复时复用摘要字符串。")},
    "holdout_agent_003": {"d193e9fb2fc7f3987120298b": (2, "明确频繁压缩破坏缓存、应接近阈值时批量压缩。")},
    "holdout_agent_010": {"112e95bff3b0844003cc5421": (2, "明确工具调用后应紧跟结果与用户异步打断之间的格式冲突。")},
    "holdout_agent_014": {"022d77b0878dbbf54469f931": (2, "明确只靠静音等待会把思考停顿误判为说完，说明判断不可靠。")},
    "holdout_agent_015": {"faaf0a40683e461537a5d2c5": (1, "给出 BM25 词频和文档频率权重的具体一侧，未解释学习型稀疏分支。")},
    "holdout_agent_017": {"078e5ee1c250bc2df5e86375": (1, "显示幻觉触发 Veto 一票否决，仅覆盖三项中的一项。")},
    "holdout_agent_025": {
        "e2955493a03f32eb06b66044": (2, "完整列出工作目录、命令黑名单与外部 API 配额速率三项限制。"),
        "1656974d99247180f1f6082b": (2, "跨页重复片段仍完整列出三项限制。"),
    },
    "holdout_agent_029": {"597ccd2ebda4e655c0828348": (2, "图示明确首次加载有 cache_creation 代价，后续轮次命中缓存。")},
    "holdout_agent_031": {"c409576cceb48cb638fa1988": (1, "展示上下文感知检索的索引期前缀，只支持对比的一半。")},
    "holdout_agent_035": {"2230f4cdc906d80a62dbf41d": (2, "明确预训练、SFT、RL 是相继阶段而非三选一。")},
}

if len(sys.argv) > 1 and sys.argv[1] == "build":
    assert selected <= {r["query_id"] for r in all_reviews}
    assert Counter((r["domain"], r["kind"]) for r in reviews) == {
        (domain, kind): 6 for domain in ("apple_support", "agent_engineering")
        for kind in ("semantic", "lexical", "confusing")
    }
    proposed_queries = []
    proposed_qrels = []
    supporting_decisions = []
    selection_decisions = []
    for r in all_reviews:
        qid = r["query_id"]
        is_selected = qid in selected
        selection_decisions.append({
            "query_id": qid, "domain": r["domain"], "kind": r["kind"],
            "decision": "selected_by_codex" if is_selected else "not_selected",
            "reason": "distinct answer, evidence and topic balance" if is_selected else (
                "high Dev intent overlap" if qid == "holdout_apple_022" else "six-per-cell balance and topic diversity"
            ),
        })
        if not is_selected:
            continue
        proposed_queries.append({k: r[k] for k in ("query_id", "query", "domain", "kind")}
                                | {"status": "codex_selected_not_gold"})
        for qrel in r["candidate_qrels"]:
            proposed_qrels.append(qrel | {"status": "codex_reviewed_not_gold", "judgment_source": "original_candidate_rechecked"})
        flagged = {m["chunk_id"] for m in r["possible_missing_supporting_chunks"]}
        assert set(extra.get(qid, {})) <= flagged
        for m in r["possible_missing_supporting_chunks"]:
            cid = m["chunk_id"]
            judgment = extra.get(qid, {}).get(cid)
            supporting_decisions.append({
                "query_id": qid, "chunk_id": cid,
                "decision": "include" if judgment else "exclude",
                "relevance": judgment[0] if judgment else 0,
                "rationale": judgment[1] if judgment else "仅主题或长句重叠，不能支持本题要求的事实。",
                "content_sha256": hashlib.sha256(corpus[cid]["content"].encode()).hexdigest(),
            })
            if judgment:
                proposed_qrels.append({
                    "query_id": qid, "chunk_id": cid, "domain": r["domain"],
                    "relevance": judgment[0], "rationale": judgment[1],
                    "source": corpus[cid]["source"], "heading_path": corpus[cid]["heading_path"],
                    "status": "codex_reviewed_not_gold", "judgment_source": "flagged_supporting_chunk_review",
                })
    assert len(proposed_queries) == 36 and len(selection_decisions) == 84
    assert len({(q["query_id"], q["chunk_id"]) for q in proposed_qrels}) == len(proposed_qrels)
    assert len(supporting_decisions) == sum(len(r["possible_missing_supporting_chunks"]) for r in reviews)
    for name, records in (("selection_decisions.jsonl", selection_decisions),
                          ("proposed_queries.jsonl", proposed_queries),
                          ("proposed_qrels.jsonl", proposed_qrels),
                          ("supporting_chunk_decisions.jsonl", supporting_decisions)):
        (out / name).write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
    grade_counts = Counter(q["relevance"] for q in proposed_qrels)
    report = [
        "# Holdout 36 题代选记录", "",
        "状态：Codex 已代选并复核已标记的补充片段；**尚未冻结为 gold**。候选原始文件保持不变。", "",
        "- 六个 domain/kind 单元各 6 题；Apple 18，Agent 18。",
        f"- 暂定 qrel {len(proposed_qrels)} 条：grade 2 = {grade_counts[2]}，grade 1 = {grade_counts[1]}。",
        f"- 所选题中已逐条处理 {len(supporting_decisions)} 条疑似遗漏片段：补入 {sum(d['decision'] == 'include' for d in supporting_decisions)}，排除 {sum(d['decision'] == 'exclude' for d in supporting_decisions)}。详见 supporting_chunk_decisions.jsonl。",
        "- 排除 holdout_apple_022（与 Dev apple_012 意图高度重叠）；其余入选题核对了候选证据、最近 Dev 题和主题分布。",
        "- 这是与出题同一 Agent 的复核，blindness=partial；标记片段来自字面重叠扫描，未证明全语料 qrel 穷尽性。",
        "- 本轮未运行检索、计算指标或改动生产代码与既有 Dev 集。", "",
        "| 领域 | 类型 | 入选题 |", "|---|---|---|",
    ]
    for domain in ("apple_support", "agent_engineering"):
        for kind in ("semantic", "lexical", "confusing"):
            entries = [r for r in proposed_queries if r["domain"] == domain and r["kind"] == kind]
            report.append(f"| {domain} | {kind} | " + "<br>".join(f"{r['query_id']} {r['query']}" for r in entries) + " |")
    report += ["", "输入：candidate_review.jsonl、candidate_qrels.jsonl、冻结 chunks.jsonl；输出：selection_decisions.jsonl、proposed_queries.jsonl、proposed_qrels.jsonl、supporting_chunk_decisions.jsonl。", ""]
    (out / "selection_report.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps({"selected": len(proposed_queries), "qrels": len(proposed_qrels),
                      "flagged_supporting_reviewed": len(supporting_decisions),
                      "included": sum(d["decision"] == "include" for d in supporting_decisions)}, ensure_ascii=False))
    sys.exit(0)

if len(sys.argv) > 1 and sys.argv[1] == "check":
    queries = rows(out / "proposed_queries.jsonl")
    qrels = rows(out / "proposed_qrels.jsonl")
    decisions = rows(out / "supporting_chunk_decisions.jsonl")
    assert len(queries) == 36 and len({q["query_id"] for q in queries}) == 36
    assert Counter((q["domain"], q["kind"]) for q in queries) == {
        (domain, kind): 6 for domain in ("apple_support", "agent_engineering")
        for kind in ("semantic", "lexical", "confusing")
    }
    ids = {q["query_id"] for q in queries}
    assert "holdout_apple_022" not in ids
    assert {r["query_id"] for r in qrels} == ids
    assert len({(r["query_id"], r["chunk_id"]) for r in qrels}) == len(qrels)
    assert all(r["query_id"] in ids and r["chunk_id"] in corpus and r["relevance"] in (1, 2)
               and r["domain"] == corpus[r["chunk_id"]]["domain"]
               and r["source"] == corpus[r["chunk_id"]]["source"] for r in qrels)
    flagged = {(r["query_id"], m["chunk_id"]) for r in reviews for m in r["possible_missing_supporting_chunks"]}
    assert {(d["query_id"], d["chunk_id"]) for d in decisions} == flagged
    assert all(((d["query_id"], d["chunk_id"]) in {(r["query_id"], r["chunk_id"]) for r in qrels})
               == (d["decision"] == "include") for d in decisions)
    assert all(hashlib.sha256(corpus[d["chunk_id"]]["content"].encode()).hexdigest() == d["content_sha256"] for d in decisions)
    print(json.dumps({"status": "PASS", "queries": len(queries), "qrels": len(qrels),
                      "flagged_reviewed": len(decisions), "gold_frozen": False}))
    sys.exit(0)

if len(sys.argv) > 1 and sys.argv[1] == "groups":
    qrels = rows(out / "proposed_qrels.jsonl")
    for r in reviews:
        evidence = [q for q in qrels if q["query_id"] == r["query_id"]]
        if len(evidence) < 2:
            continue
        print(f"\n{r['query_id']} {r['query']}")
        for q in evidence:
            print(f"  {q['chunk_id']} grade={q['relevance']} {q['rationale']}")
    sys.exit(0)

for r in reviews:
    if len(sys.argv) > 1 and sys.argv[1].startswith("holdout_") and r["query_id"] != sys.argv[1]:
        continue
    if len(sys.argv) > 1 and sys.argv[1] in ("apple", "agent") and not r["query_id"].startswith(f"holdout_{sys.argv[1]}_"):
        continue
    if len(sys.argv) > 2 and sys.argv[1] == "missing" and not r["query_id"].startswith(f"holdout_{sys.argv[2]}_"):
        continue
    leak = leakage[r["query_id"]]
    print(f"\n## {r['query_id']} | {r['kind']} | {r['query']}")
    print(f"intent={r['semantic_intention_overlap']['assessment']} flags={','.join(leak['review_flag'])} missing={len(r['possible_missing_supporting_chunks'])}")
    print(f"nearestDev={leak['nearest_dev_query']}")
    if len(sys.argv) > 1 and sys.argv[1] == "missing":
        for m in r["possible_missing_supporting_chunks"]:
            print(f"\nMISSING? {m['chunk_id']} {m['source']}\n{corpus[m['chunk_id']]['content']}")
        continue
    if len(sys.argv) > 1 and sys.argv[1] == "summary":
        print("qrels=" + "; ".join(f"{q['relevance']}:{q['rationale']}" for q in r["candidate_qrels"]))
        continue
    for q in r["candidate_qrels"]:
        print(f"\nGRADE {q['relevance']} {q['chunk_id']} {q['source']}\nRationale: {q['rationale']}\n{corpus[q['chunk_id']]['content']}")
    for m in r["possible_missing_supporting_chunks"]:
        print(f"\nMISSING? {m['chunk_id']} {m['source']}\n{corpus[m['chunk_id']]['content']}")
