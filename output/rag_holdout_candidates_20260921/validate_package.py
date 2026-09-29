"""Structural/evidence checks only; no model inference or retrieval."""
import json
from collections import Counter
from pathlib import Path

from build_candidates import OUT, INDEX, digest, protected_hashes, read_lines, similarity

queries = read_lines(OUT / 'candidate_queries.jsonl')
qrels = read_lines(OUT / 'candidate_qrels.jsonl')
reviews = read_lines(OUT / 'candidate_review.jsonl')
evidence = {r['chunk_id']: r for r in read_lines(OUT / 'chunk_evidence.jsonl')}
manifest = json.loads((OUT / 'generation_manifest.json').read_text(encoding='utf-8'))
leakage = json.loads((OUT / 'leakage_report.json').read_text(encoding='utf-8'))['queries']
corpus = {c['chunk_id']: c for domain in manifest['corpus_source_sha256'] for c in read_lines(INDEX / domain / 'chunks.jsonl')}
ids = {q['query_id'] for q in queries}
assert len(ids) == len(queries) == len(reviews) == len(leakage) == 84
assert sorted(Counter((q['domain'], q['kind']) for q in queries).values()) == [14] * 6
assert len({(r['query_id'], r['chunk_id']) for r in qrels}) == len(qrels)
for qrel in qrels:
    assert qrel['query_id'] in ids and qrel['chunk_id'] in corpus
    chunk = corpus[qrel['chunk_id']]
    assert qrel['domain'] == chunk['domain']
    assert qrel['source'] == chunk['source'] and qrel['heading_path'] == chunk['heading_path']
    assert qrel['relevance'] in (1, 2) and qrel['rationale'].strip()
    assert evidence[qrel['chunk_id']]['content'] == chunk['content']
for query, review, leak in zip(queries, reviews, leakage):
    assert query['query_id'] == review['query_id'] == leak['query_id']
    assert review['candidate_qrels'] == [r for r in qrels if r['query_id'] == query['query_id']]
    assert review['human_decision'] == 'pending'
    assert all(c['chunk_id'] in corpus and c['relevance'] is None for c in review['possible_missing_supporting_chunks'])
    assert len(leak['all_dev_comparisons']) == 60
    assert leak['nearest_dev_query'] == max(leak['all_dev_comparisons'], key=lambda r: r['semantic_cosine'])
    assert all(-1.001 <= r['semantic_cosine'] <= 1.001 for r in leak['all_dev_comparisons'])
    if review['answer_mode'] == 'COMPOSITIONAL':
        assert len(review['candidate_qrels']) >= 2 and all(r['relevance'] == 1 for r in review['candidate_qrels'])
assert not any(r['exact_duplicate'] or r['normalized_duplicate'] for r in leakage)
internal_lexical_pairs = []
for i, query in enumerate(queries):
    for j in range(i + 1, len(queries)):
        score = similarity(query['query'], queries[j]['query'])
        if score >= .75:
            internal_lexical_pairs.append([query['query_id'], queries[j]['query_id'], score])
            assert 'candidate_internal_similarity_review' in leakage[i]['review_flag']
            assert 'candidate_internal_similarity_review' in leakage[j]['review_flag']
assert manifest['blindness'] == 'partial' and manifest['known_dev_history_exposure']
assert not manifest['retrieval_rankings_used'] and not manifest['human_approved']
before = json.loads((OUT / 'protected_hashes_before.json').read_text(encoding='utf-8'))
assert before == protected_hashes()
assert all(digest(OUT / name) == sha for name, sha in manifest['output_hashes'].items())
assert not Path('benchmarks/rag_holdout_v1').exists()
result = {'status': 'PASS', 'query_count': 84, 'qrel_count': len(qrels),
          'internal_lexical_pairs_checked': 3486, 'internal_lexical_pairs_above_threshold': internal_lexical_pairs,
          'protected_files_unchanged': len(before), 'all_candidate_chunks_verified_against_corpus': True,
          'every_query_has_60_dev_comparisons': True, 'gold_created': False,
          'semantic_correctness': 'pending independent GPT and final human adjudication'}
(OUT / 'validation.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
print(json.dumps(result, indent=2))
