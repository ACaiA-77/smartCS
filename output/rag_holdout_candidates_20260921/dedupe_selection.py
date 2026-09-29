"""Group equivalent evidence without discarding valid retrieved chunk IDs."""
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
root = Path(__file__).resolve().parents[2]
out = root / "artifacts/rag_holdout_candidates_20260921"


def read(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def write(path, values):
    path.write_text("".join(json.dumps(v, ensure_ascii=False) + "\n" for v in values), encoding="utf-8")


# Query-local duplicate answer facts. The first ID is the focused representative.
# Partial overlapping chunks retain their original grade in the alias record.
EQUIVALENT = {
    "holdout_apple_004": {"817015c82071304173546fa0": ["5fa027f543c0c06bd2695405"]},
    "holdout_apple_009": {"1bf1baf9a596abdc8b758e6a": ["e5de95b7e43349db6394ddc1"]},
    "holdout_apple_010": {"ad4720d0794f2a8a782da130": ["e5de95b7e43349db6394ddc1"]},
    "holdout_apple_012": {"423350623477140fab76a473": ["e57294bfadd3874712844345"]},
    "holdout_apple_013": {"54616d811d87da681ff1ed18": ["f329da807aaa61117276a76f"]},
    "holdout_apple_023": {"22d974e94efa69e7ff90264b": ["32e828b1d9ddf35af91b3905"]},
    "holdout_apple_025": {"bfe2d5cd6455e79336ccc468": ["23e98067124461c526c7af0e"]},
    "holdout_apple_033": {"55872851378be425cd284df1": ["e57294bfadd3874712844345"]},
    "holdout_apple_039": {"b929d2df28d8f35d0e070218": ["0805f1d09137aa20687d5e89"]},
    "holdout_apple_040": {"27bf9f65f436437fb5be86c7": ["436ffb0f457b7696ce2531aa"]},
    "holdout_agent_003": {"7850da53a3707eeac2994db2": ["d193e9fb2fc7f3987120298b"]},
    "holdout_agent_010": {"7965f07400902390c8037d70": ["112e95bff3b0844003cc5421", "4591f09d83fc04614edb56d8"]},
    "holdout_agent_014": {"b9ada767564a3bc08406099f": ["022d77b0878dbbf54469f931"]},
    "holdout_agent_025": {"b7caa22a8eda83639bdd1b95": ["e2955493a03f32eb06b66044", "1656974d99247180f1f6082b"]},
    "holdout_agent_029": {"0e8fa54da74fdf92b1390143": ["597ccd2ebda4e655c0828348"]},
    "holdout_agent_035": {"36b7c633a6860142bfc6e461": ["2230f4cdc906d80a62dbf41d"]},
}

# The GPT read-only audit checked the complete diagram chunk and upgraded it.
# proposed_qrels.jsonl remains the historical, unmodified judgment.
RELEVANCE_OVERRIDES = {
    ("holdout_agent_010", "4591f09d83fc04614edb56d8"): (2, "图示完整说明 tool_call 后必须跟 tool_result，而用户异步打断造成格式冲突。"),
}

raw_qrels = read(out / "proposed_qrels.jsonl")
qrels = []
for original in raw_qrels:
    qrel = original.copy()
    override = RELEVANCE_OVERRIDES.get((qrel["query_id"], qrel["chunk_id"]))
    if override:
        qrel["original_relevance"] = qrel["relevance"]
        qrel["relevance"], qrel["adjudication_reason"] = override
    qrels.append(qrel)
by_pair = {(r["query_id"], r["chunk_id"]): r for r in qrels}
assert len(by_pair) == len(qrels)
alias_to_canonical = {}
for qid, groups in EQUIVALENT.items():
    for canonical, aliases in groups.items():
        assert (qid, canonical) in by_pair
        for alias in aliases:
            assert (qid, alias) in by_pair and (qid, alias) not in alias_to_canonical
            assert alias != canonical
            alias_to_canonical[(qid, alias)] = canonical

groups = defaultdict(list)
for qrel in qrels:
    qid, cid = qrel["query_id"], qrel["chunk_id"]
    canonical = alias_to_canonical.get((qid, cid), cid)
    groups[(qid, canonical)].append(qrel)

representatives = []
aliases = []
group_records = []
for (qid, canonical), members in groups.items():
    leader = by_pair[(qid, canonical)]
    representatives.append(leader | {"role": "canonical_representative_not_evaluator_ready"})
    group_records.append({
        "query_id": qid, "group_id": f"{qid}:{canonical}",
        "canonical_chunk_id": canonical, "relevance": leader["relevance"],
        "member_chunk_ids": [r["chunk_id"] for r in members],
        "members": [{"chunk_id": r["chunk_id"], "relevance": r["relevance"],
                     **({"original_relevance": r["original_relevance"]} if "original_relevance" in r else {}),
                     **({"adjudication_reason": r["adjudication_reason"]} if "adjudication_reason" in r else {}),
                     "role": "canonical" if r["chunk_id"] == canonical else "equivalent_alias"}
                    for r in members],
        "rationale": leader["rationale"],
    })
    for member in members:
        if member["chunk_id"] != canonical:
            aliases.append(member | {"role": "equivalent_non_scoring_evidence",
                                     "canonical_chunk_id": canonical,
                                     "group_id": f"{qid}:{canonical}"})

assert len(representatives) + len(aliases) == len(qrels) == 62
assert len(groups) == len(representatives) == 44 and len(aliases) == 18
assert all(len({m["relevance"] for m in g["members"]}) == 1 for g in group_records)

if len(sys.argv) > 1 and sys.argv[1] == "build":
    write(out / "qrel_equivalence_groups.jsonl", group_records)
    write(out / "canonical_representatives.jsonl", representatives)
    write(out / "equivalent_non_scoring_evidence.jsonl", aliases)
    report = [
        "# 答案事实级 qrel 去重审查", "",
        "GPT iteration 2: 36 题选择通过；重复证据需从 Recall/nDCG 的独立分母项中移出。",
        f"原始相关证据 {len(qrels)} 条保留；按事实分成 {len(groups)} 组，其中 {len(aliases)} 条为等价或部分重叠的非独立证据。",
        "36 道题和 proposed_qrels.jsonl 原样保留。canonical_representatives.jsonl 是审查视图，**不可直接交给当前 evaluator 计算正式指标**。",
        "原因：rag/evaluation/metrics.py 只认 qrels 中的 chunk_id；命中有效 alias 却未命中 canonical 时，当前 Recall/MRR/nDCG 都会错误计为 0。正式评测前必须采用事实组计分，同时让组内任一有效 chunk 得到相应 relevance 的命中信用。",
        "同一事实组的所有成员等级相同。Apple 023 的截断片段单列 grade 1；Agent 010 的图示片段从原始 grade 1 复核为 grade 2，组记录保留 original_relevance。",
        "Apple 029 的两段 grade 1 证据分别支持退款权限和订阅取消权限，保留为两个事实组；Apple 004 的 grade 1 下一步操作也保留独立。",
        "本轮未修改生产 evaluator、未运行检索、未冻结 gold。", "",
        "| Query | Canonical | Equivalent aliases |", "|---|---|---|",
    ]
    for group in group_records:
        members = group["members"]
        if len(members) > 1:
            report.append(f"| {group['query_id']} | {group['canonical_chunk_id']} (grade {group['relevance']}) | " +
                          "<br>".join(f"{m['chunk_id']} (grade {m['relevance']})" for m in members if m["role"] == "equivalent_alias") + " |")
    (out / "qrel_equivalence_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps({"status": "PASS", "questions": 36, "evidence": len(qrels),
                      "fact_groups": len(groups), "aliases": len(aliases), "gold_frozen": False}))
elif len(sys.argv) > 1 and sys.argv[1] == "check":
    saved_groups = read(out / "qrel_equivalence_groups.jsonl")
    saved_representatives = read(out / "canonical_representatives.jsonl")
    saved_aliases = read(out / "equivalent_non_scoring_evidence.jsonl")
    assert saved_groups == group_records and saved_representatives == representatives and saved_aliases == aliases
    assert {(g["query_id"], m["chunk_id"]) for g in saved_groups for m in g["members"]} == set(by_pair)
    assert all(max(m["relevance"] for m in g["members"]) == g["relevance"] for g in saved_groups)
    assert all(len({m["relevance"] for m in g["members"]}) == 1 for g in saved_groups)
    assert all(g["member_chunk_ids"] == [m["chunk_id"] for m in g["members"]] for g in saved_groups)
    sys.path.insert(0, str(root))
    from rag.evaluation.metrics import evaluate_ranking
    sample = saved_aliases[0]
    canonical = sample["canonical_chunk_id"]
    grade = by_pair[(sample["query_id"], canonical)]["relevance"]
    assert evaluate_ranking([sample["chunk_id"]], {canonical: grade}, cutoffs=(10,))["recall@10"] == 0.0
    assert evaluate_ranking([canonical], {canonical: grade}, cutoffs=(10,))["recall@10"] == 1.0
    print(json.dumps({"status": "PASS", "fact_groups": len(groups), "aliases": len(aliases),
                      "alias_only_recall_with_current_evaluator": 0.0,
                      "canonical_only_recall_with_current_evaluator": 1.0}))
