"""Package human-review candidates and query-only leakage signals. Never run RAG."""
import hashlib
import json
import os
import re
import subprocess
import sys
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from authoring_data import AGENT, APPLE, COMPOSITIONAL

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'artifacts/rag_holdout_candidates_20260921'
INDEX = ROOT / 'artifacts/rag_round3/production_indexes'
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding='utf-8')


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def write_json(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def write_lines(name, rows):
    (OUT / name).write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold() if c.isalnum())


def similarity(a, b):
    return SequenceMatcher(None, normalize(a), normalize(b), autojunk=False).ratio()


def protected_hashes():
    paths = set((ROOT / 'benchmarks/rag').glob('*'))
    for folder in ('agents', 'api', 'mcp', 'rag', 'memory', 'auth', 'checkpoint', 'refunds', 'tickets', 'sandbox', 'tracing', 'scripts', 'tests'):
        paths.update((ROOT / folder).rglob('*.py'))
    for domain in APPLE_DOMAIN, AGENT_DOMAIN:
        paths.update(INDEX.joinpath(domain).glob('*'))
    return {str(p.relative_to(ROOT)).replace('\\', '/'): digest(p) for p in sorted(paths) if p.is_file()}


APPLE_DOMAIN, AGENT_DOMAIN = 'apple_support', 'agent_engineering'

# Candidate annotator's intent comparison to all 60 Dev questions, not system results.
# Same document/chunk alone is never used as a rejection rule.
INTENT_NOTES = {
    'holdout_apple_001': ('partial', ['apple_010', 'apple_012'], '恢复到期后未收到通知的补救，与等待时长/查询进度相关但不同。'),
    'holdout_apple_002': ('partial', ['apple_011', 'apple_028'], '同为取消恢复，但这里是未授权请求，不是本人记起密码。'),
    'holdout_apple_003': ('partial', ['apple_017', 'apple_029'], '取消协助的材料准备，区别于查保修或按月取消后的保障。'),
    'holdout_apple_007': ('partial', ['apple_023', 'apple_026'], '数字内容获批与到账的区别，可能与退款到账时点共享意图。'),
    'holdout_apple_019': ('partial', ['apple_004'], '同App Store注册，问缺失创建入口，不问支付选项/验证。'),
    'holdout_apple_020': ('partial', ['apple_002'], '智能电视特定跳转地址是注册入口总题的子意图。'),
    'holdout_apple_021': ('partial', ['apple_010'], '恢复确认的通知渠道，区别于等待期长度。'),
    'holdout_apple_022': ('high', ['apple_012'], '恢复进度入口与所需账户信息高度相关，建议排除出最终holdout或由人工裁定。'),
    'holdout_apple_023': ('partial', ['apple_024', 'apple_025'], 'Edition产品特定检测要求，区别于通用渠道及配件退货。'),
    'holdout_apple_024': ('partial', ['apple_021'], '蔡司两版专门规则，可能与通用14天期限重叠。'),
    'holdout_apple_032': ('partial', ['apple_018'], '维修估价主题相近，这里问授权商是否必须采用Apple报价。'),
    'holdout_apple_034': ('partial', ['apple_023'], '加急运费不退是退款范围的独立子事实。'),
    'holdout_apple_035': ('partial', ['apple_021', 'apple_025'], '问不可退商品清单，不是普通退货期限。'),
    'holdout_apple_036': ('partial', ['apple_025'], '官网设备安全功能未关闭的退货限制，区别于门店配件。'),
    'holdout_apple_037': ('partial', ['apple_025'], '用户数据擦除责任，区别于配件是否能退。'),
    'holdout_apple_042': ('partial', ['apple_029'], '预付固定期限且第三方购买，与按月/年向Apple付费不同。'),
    'holdout_agent_005': ('partial', ['agent_021', 'agent_027'], '文件工具范围读取的具体设计，邻近系统上下文/按需加载的大主题。'),
    'holdout_agent_007': ('partial', ['agent_026'], '业务经验的动态非公开性，邻近知识库定位但不问service-knowledge。'),
    'holdout_agent_008': ('partial', ['agent_030'], '运行时多Agent协调职责，区别于架构知识库的未来应用。'),
    'holdout_agent_018': ('none', [], 'AdaptThink采样冷启动，Dev未覆盖后训练。'),
    'holdout_agent_027': ('none', [], '连续多模态观察接口，Dev未覆盖Computer Use。'),
    'holdout_agent_030': ('partial', ['agent_023'], '检索技术作用层次，区别于先划分业务领域。'),
    'holdout_agent_032': ('partial', ['agent_012'], '虽出现Hooks，核心是时间触发与任意外部事件即时接入。'),
    'holdout_agent_039': ('partial', ['agent_001', 'agent_002'], '记忆投毒的持久性，邻近约束话题但不是Hook与Prompt比较。'),
}


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    if (OUT / 'generation_manifest.json').exists():
        raise FileExistsError('Candidate package already exists; preserve it for review.')
    before = protected_hashes()
    write_json('protected_hashes_before.json', before)
    manifest = json.loads((ROOT / 'benchmarks/rag/benchmark_manifest.json').read_text(encoding='utf-8'))
    assert manifest['benchmark_version'] == 'rag-round3-v4-qrel-audited'
    for domain, expected in manifest['source_sha256'].items():
        assert digest(INDEX / domain / 'chunks.jsonl') == expected, f'Frozen corpus changed: {domain}'
    corpora = {d: read_lines(INDEX / d / 'chunks.jsonl') for d in (APPLE_DOMAIN, AGENT_DOMAIN)}
    dev = read_lines(ROOT / 'benchmarks/rag/queries.jsonl')
    dev_qrels = read_lines(ROOT / 'benchmarks/rag/qrels.jsonl')
    assert len(dev) == 60 and len(dev_qrels) == 95
    dev_sets = {q['query_id']: {r['chunk_id'] for r in dev_qrels if r['query_id'] == q['query_id']} for q in dev}
    queries, qrels, reviews = [], [], []
    evidence = {}
    for domain, groups, prefix in ((APPLE_DOMAIN, APPLE, 'apple'), (AGENT_DOMAIN, AGENT, 'agent')):
        number = 0
        for kind, specifications in groups.items():
            assert len(specifications) == 14
            for position, (index, query, rationale, extra) in enumerate(specifications):
                number += 1
                query_id = f'holdout_{prefix}_{number:03}'
                compositional = (domain, kind, position) in COMPOSITIONAL
                q = dict(query_id=query_id, query=query, domain=domain, kind=kind, status='candidate_not_gold')
                queries.append(q)
                annotations = [(index, 1 if compositional else 2, rationale), *extra]
                labels = []
                for chunk_index, relevance, why in annotations:
                    chunk = corpora[domain][chunk_index]
                    assert chunk['domain'] == domain and relevance in (1, 2) and why.strip()
                    label = dict(query_id=query_id, chunk_id=chunk['chunk_id'], domain=domain,
                                 relevance=relevance, rationale=why, source=chunk['source'],
                                 heading_path=chunk['heading_path'], status='candidate_not_gold')
                    labels.append(label)
                    evidence[chunk['chunk_id']] = {**chunk, 'corpus_row': chunk_index, 'content_sha256': hashlib.sha256(chunk['content'].encode()).hexdigest()}
                assert len({r['chunk_id'] for r in labels}) == len(labels)
                assert not compositional or all(r['relevance'] == 1 for r in labels)
                qrels.extend(labels)
                overlap, related, reason = INTENT_NOTES.get(query_id, ('none', [], '与全部Dev题比较：本题考查具体新事实或机制；未发现相同问题意图。此为候选标注员判断，仍需人工复核。'))
                reviews.append({**q, 'candidate_qrels': labels, 'answer_mode': 'COMPOSITIONAL' if compositional else 'DIRECT',
                                'confidence': 'medium' if compositional or overlap in ('partial', 'high') or domain == AGENT_DOMAIN else 'high',
                                'semantic_intention_overlap': {'assessment': overlap, 'related_dev_ids': related, 'rationale': reason, 'reviewer': 'candidate_author_not_independent'},
                                'possible_missing_supporting_chunks': [], 'supporting_search_status': 'literal_overlap_scan_not_exhaustive',
                                'human_decision': 'pending', 'human_reviewer': None})
    assert len(queries) == 84 and len({q['query_id'] for q in queries}) == 84
    assert len({normalize(q['query']) for q in queries}) == 84
    # Literal corpus inspection only: surface adjacent/duplicate evidence for adjudication;
    # never promote these unjudged matches to qrels or use a retrieval model/rank.
    for review in reviews:
        known = {r['chunk_id'] for r in review['candidate_qrels']}
        primary = evidence[review['candidate_qrels'][0]['chunk_id']]['content']
        sentences = [normalize(s) for s in re.split(r'[。！？\n]', primary)]
        probes = [s for s in sentences if len(s) >= 30][:3]
        for chunk in corpora[review['domain']]:
            if chunk['chunk_id'] in known:
                continue
            body = normalize(chunk['content'])
            if any(probe in body for probe in probes):
                review['possible_missing_supporting_chunks'].append({'chunk_id': chunk['chunk_id'], 'source': chunk['source'], 'reason': '与主候选正文含相同长句；相关性尚未判定，不是自动qrel', 'relevance': None})
    write_lines('candidate_queries.jsonl', queries)
    write_lines('candidate_qrels.jsonl', qrels)
    write_lines('candidate_review.jsonl', reviews)
    write_lines('chunk_evidence.jsonl', list(evidence.values()))
    print('84 candidates fixed; starting query-only embedding similarity (no corpus embedding/search).', flush=True)
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    from rag.embeddings import SentenceTransformerEmbeddingBackend
    import numpy as np
    backend = SentenceTransformerEmbeddingBackend()
    assert backend.dimension == 1024
    vectors = np.asarray(backend.embed_batch([q['query'] for q in queries + dev]))
    assert vectors.shape == (144, 1024) and np.isfinite(vectors).all()
    scores = vectors @ vectors.T
    np.save(OUT / 'query_similarity.npy', scores)
    leakage = []
    for i, (q, review) in enumerate(zip(queries, reviews)):
        comparisons = []
        candidate_set = {r['chunk_id'] for r in review['candidate_qrels']}
        for j, d in enumerate(dev):
            shared = sorted(candidate_set & dev_sets[d['query_id']])
            comparisons.append({'query_id': d['query_id'], 'query': d['query'],
                                'exact_duplicate': q['query'] == d['query'],
                                'normalized_duplicate': normalize(q['query']) == normalize(d['query']),
                                'lexical_similarity': similarity(q['query'], d['query']),
                                'semantic_cosine': float(scores[i, 84 + j]),
                                'qrel_overlap': shared,
                                'qrel_jaccard': len(shared) / len(candidate_set | dev_sets[d['query_id']])})
        nearest = sorted(comparisons, key=lambda r: r['semantic_cosine'], reverse=True)[:3]
        peers = sorted([{'query_id': other['query_id'], 'query': other['query'],
                         'semantic_cosine': float(scores[i, j]), 'lexical_similarity': similarity(q['query'], other['query']),
                         'normalized_duplicate': normalize(q['query']) == normalize(other['query'])}
                        for j, other in enumerate(queries) if j != i], key=lambda r: r['semantic_cosine'], reverse=True)[:3]
        flags = []
        if any(c['normalized_duplicate'] for c in comparisons):
            flags.append('exclude_exact_or_normalized_dev_duplicate')
        if max(c['lexical_similarity'] for c in comparisons) >= .75:
            flags.append('dev_lexical_near_duplicate_review')
        if nearest[0]['semantic_cosine'] >= .80:
            flags.append('dev_high_semantic_similarity_review')
        if review['semantic_intention_overlap']['assessment'] != 'none':
            flags.append('dev_intention_overlap_review')
        if peers[0]['semantic_cosine'] >= .80 or any(p['lexical_similarity'] >= .75 for p in peers):
            flags.append('candidate_internal_similarity_review')
        if review['semantic_intention_overlap']['assessment'] == 'high':
            flags.append('author_recommends_exclusion_pending_human')
        if not flags:
            flags.append('pending_human_semantic_review')
        leakage.append({'query_id': q['query_id'], 'exact_duplicate': any(c['exact_duplicate'] for c in comparisons),
                        'normalized_duplicate': any(c['normalized_duplicate'] for c in comparisons),
                        'lexical_similarity': max(c['lexical_similarity'] for c in comparisons),
                        'semantic_intention_overlap': review['semantic_intention_overlap'],
                        'nearest_dev_query': nearest[0], 'nearest_dev_queries': nearest,
                        'qrel_overlap': [c for c in comparisons if c['qrel_overlap']],
                        'nearest_candidate_queries': peers, 'review_flag': flags,
                        'all_dev_comparisons': comparisons})
    write_json('leakage_report.json', {'method': 'NFKC/alnum/casefold + SequenceMatcher + BGE-M3 query-query cosine + author intent audit',
        'thresholds': {'lexical': .75, 'semantic': .80}, 'thresholds_are_heuristic_not_validated': True,
        'semantic_model': 'BAAI/bge-m3', 'dimension': 1024, 'fake': False,
        'scope': '144 query strings only; no corpus vectors, retrievers, ranks or RAG metrics',
        'no_flag_is_not_independence_proof': True, 'queries': leakage})
    for domain in (APPLE_DOMAIN, AGENT_DOMAIN):
        sections = ['# 候选人工审核表 — ' + domain, '\n尚非 gold。先看问题与原文，再审核相关性及 Dev 泄漏。填写意见到独立 review_decisions 文件，勿覆盖候选证据。\n']
        for r, leak in zip(reviews, leakage):
            if r['domain'] != domain:
                continue
            sections += [f"## {r['query_id']} | {r['kind']} | confidence={r['confidence']}", r['query'],
                         f"模式：{r['answer_mode']}。人工决定：待审核。", '标记：' + ', '.join(leak['review_flag']),
                         '意图审核：' + r['semantic_intention_overlap']['rationale'],
                         '最相近 Dev：' + leak['nearest_dev_query']['query_id'] + ' — ' + leak['nearest_dev_query']['query']]
            for label in r['candidate_qrels']:
                chunk = evidence[label['chunk_id']]
                sections += [f"### relevance={label['relevance']} | `{label['chunk_id']}`", label['rationale'],
                             f"来源：{label['source']} | chunk正文（冻结语料，非当前政策/产品保证）：", chunk['content']]
            if r['possible_missing_supporting_chunks']:
                sections += ['还需检查的未标注长句重叠片段：' + ', '.join(c['chunk_id'] for c in r['possible_missing_supporting_chunks'])]
        (OUT / f'review_{domain}.md').write_text('\n\n'.join(sections) + '\n', encoding='utf-8')
    counts = Counter((q['domain'], q['kind']) for q in queries)
    summary = {'candidate_queries': len(queries), 'candidate_qrels': len(qrels),
               'by_domain_kind': {f'{d}/{k}': n for (d, k), n in counts.items()},
               'relevance': dict(Counter(r['relevance'] for r in qrels)),
               'qrel_source_counts': dict(Counter(r['source'] for r in qrels)),
               'review_flags': dict(Counter(flag for r in leakage for flag in r['review_flag'])),
               'exact_dev_duplicates': sum(r['exact_duplicate'] for r in leakage),
               'normalized_dev_duplicates': sum(r['normalized_duplicate'] for r in leakage),
               'human_approved': 0, 'target_final_queries': 36, 'gold_frozen': False}
    write_json('coverage_summary.json', summary)
    assert before == protected_hashes(), 'Protected source or frozen inputs changed'
    write_json('protected_hashes_after.json', protected_hashes())
    write_json('generation_manifest.json', {'task_id': 'smartcs_holdout_candidates_20260921', 'iteration': 1,
        'status': 'candidate_only_not_gold', 'blindness': 'partial', 'known_dev_history_exposure': True,
        'failure_guided_authoring': False, 'retrieval_rankings_used': False, 'production_change': False,
        'rag_executions': 0, 'retrieval_metrics_computed': False, 'human_approved': False,
        'corpus_source_sha256': manifest['source_sha256'], 'chunking_version': manifest['chunking_version'],
        'dev_sha256': {p.name: digest(p) for p in (ROOT / 'benchmarks/rag').glob('*.json*')},
        'authoring_sha256': digest(Path(__file__).with_name('authoring_data.py')),
        'candidate_generation': 'single corpus-topic authoring batch; no failure/ranking-driven replenishment',
        'source_limitations': ['Frozen corpus assertions only, not independently checked current external facts.',
            'PDF heading_path extraction contains inherited irrelevant headings; judgments use body text.',
            'Book corpus has repeated/overlapping chunks; candidate qrels are not exhaustive gold.',
            'Author saw historical Dev failures before this task; independence requires human adjudication.',
            'Query-only BGE similarity is a heuristic and may miss paraphrases or flag distinct sub-intents.'],
        'protected_file_count': len(before), 'protected_hashes_equal': True,
        'output_hashes': {p.name: digest(p) for p in OUT.iterdir() if p.is_file() and p.suffix != '.log' and not p.name.endswith('_command.json')}})
    text = '# Holdout 候选包\n\n84题，Apple/Agent各42题，每域每类14题。' + f"共{len(qrels)}条候选qrel，未冻结、无人类批准。\n\n"
    text += '按 review_apple_support.md / review_agent_engineering.md 逐题核对原文与相关性。完整泄漏信号见 leakage_report.json。\n\n'
    text += '建议排除 holdout_apple_022（与Dev恢复进度子意图高度重叠）；保留原记录供审计，不能把未标记项称作独立性已通过。最终36题需人工每域每类选6题，不足时如实报告。\n\n'
    text += '部分PDF标题继承错误；本次只按正文标注，未修语料。仅做问题之间的相似检查，未运行RAG或计算检索指标。\n\n'
    text += '既有生产源码、测试脚本、Dev及冻结索引哈希保持一致。仅新增本候选包及output目录中的复现脚本。\n\n```json\n' + json.dumps(summary, ensure_ascii=False, indent=2) + '\n```\n'
    (OUT / 'coverage_summary.md').write_text(text, encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


def self_check():
    assert normalize(' Apple-ID？ ') == normalize('apple id')
    assert similarity('ＡＰＰＬＥ！', 'apple') == 1
    assert similarity('订单退款', '向量索引') == 0
    assert set(APPLE) == set(AGENT) == {'semantic', 'lexical', 'confusing'}
    assert all(len(group) == 14 for data in (APPLE, AGENT) for group in data.values())
    assert COMPOSITIONAL == {('apple_support', 'confusing', 0)}
    print('6 authoring/normalization self-checks passed')


if __name__ == '__main__':
    self_check()
    if '--check' not in sys.argv:
        build()
