"""Offline one-shot candidate freeze; run with global Python, never on a service.
Only writes this directory. Refuses model load when physical RAM is insufficient.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
CUDA_PREP = '--cuda' in sys.argv
ENV = {
    'RAG_INDEX_ROOT': str(ROOT / 'artifacts/rag_round3/production_indexes'),
    'EMBEDDING_BACKEND': 'local', 'EMBEDDING_MODEL': 'BAAI/bge-m3',
    'RAG_SPARSE_MODE': 'global_corpus_v1', 'CUDA_VISIBLE_DEVICES': '-1',
    'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
    'RAG_RERANKER_BACKEND': 'fake', 'RAG_ALLOW_DRY_RUN': 'false',
    'PYTHON_DOTENV_DISABLED': '1', 'TOKENIZERS_PARALLELISM': 'false',
    'FAISS_INDEX_PATH': str(OUT / 'unused_legacy_index'),
}
if CUDA_PREP:
    ENV['CUDA_VISIBLE_DEVICES'] = '0'
os.environ.update(ENV)
import json
import time
import hashlib
import subprocess
from datetime import datetime, timezone
from collections import Counter


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]


def available_memory():
    import ctypes
    from ctypes import wintypes
    class Status(ctypes.Structure):
        _fields_ = [('length', wintypes.DWORD), ('load', wintypes.DWORD)] + [(key, ctypes.c_ulonglong) for key in ('total', 'available', 'totalpage', 'availablepage', 'totalvirtual', 'availablevirtual', 'extended')]
    s = Status()
    s.length = ctypes.sizeof(s)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s)):
        raise RuntimeError('Cannot verify physical memory availability')
    return {'available_physical_bytes': s.available, 'total_physical_bytes': s.total, 'memory_load_percent': s.load, 'available_commit_bytes': s.availablepage}


def snapshot(model_id):
    from huggingface_hub.constants import HF_HUB_CACHE
    base = Path(HF_HUB_CACHE) / ('models--' + model_id.replace('/', '--'))
    ref = base / 'refs/main'
    revision = ref.read_text().strip() if ref.exists() else None
    candidates = list((base / 'snapshots').glob('*'))
    def weight_path(p):
        if (p / 'model.safetensors').is_file():
            return p / 'model.safetensors'
        if model_id == 'BAAI/bge-m3' and (p / 'pytorch_model.bin').is_file():
            return p / 'pytorch_model.bin'
        return None
    valid = [p for p in candidates if weight_path(p) is not None and (p / 'config.json').is_file()]
    chosen = next((p for p in valid if p.name == revision), None)
    if chosen is None and len(valid) == 1:
        chosen = valid[0]
    if chosen is None:
        raise RuntimeError(f'No unambiguous real local snapshot for {model_id}')
    return {'model_id': model_id, 'model_path': str(chosen.resolve()), 'revision': chosen.name, 'cached_main_revision': revision, 'path_exists': chosen.is_dir(), 'weight_file': str(weight_path(chosen).resolve()), 'weight_bytes': weight_path(chosen).stat().st_size, 'config_sha256': sha(chosen / 'config.json')}


def main():
    started = time.perf_counter()
    benchmark = ROOT / 'benchmarks/rag'
    artifacts = Path(ENV['RAG_INDEX_ROOT'])
    report = {'schema_version': 1, 'task_id': 'gpu-eval-inputs', 'status': 'blocked', 'generated_at': datetime.now(timezone.utc).isoformat(),
              'requested_configuration': {'agent': 'functions.Agent', 'model': 'openai-codex/gpt-6.1-sol', 'reasoning_effort': 'medium'},
              'effective_configuration': {'agent': 'unknown', 'model': 'unknown', 'reasoning_effort': 'unknown'},
              'environment': ENV, 'python_executable': sys.executable, 'python_version': sys.version,
              'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'benchmark_root': str(benchmark), 'artifact_root': str(artifacts),
              'scoring_mode': 'ordinary_chunk_id_graded_qrels', 'group_scoring': False,
              'input_sha256': None, 'inputs_generated': False, 'reranking_performed': False,
              'limitations': ['Historical benchmark tuning/regression set, not an independent holdout.', '768 is a Unicode character budget on documents, not a token limit; query text is unchanged.', 'Only real retrieval candidates are prepared; no model quality, latency, or GPU evaluation is performed.', 'Production 9 pairs are the first 9 of each frozen RRF top20; quality uses all 20.', 'No qrels may be removed, including positives not recalled in top20.']}
    try:
        import torch
        import faiss
        report['torch_version'] = torch.__version__
        report['torch_cuda_version'] = torch.version.cuda
        if CUDA_PREP:
            assert torch.__version__ == '2.11.0+cu128' and torch.cuda.is_available()
            report['gpu_name'] = torch.cuda.get_device_name(0)
            free, total = torch.cuda.mem_get_info()
            report['gpu_memory_before_load'] = {'free_bytes': free, 'total_bytes': total}
            assert free >= 4 * 1024**3, 'Need 4 GiB free VRAM for one FP32 BGE-M3 plus short query activations'
            report['global_cpu_torch_version_previously_verified'] = '2.13.0+cpu'
            report['limitations'].append('User-approved input preparation uses torch2.11 CUDA FP32, not original torch2.13 CPU; floating-point implementation may alter dense ranks. All rerank devices share this single freeze, so their comparison is valid but not a variable-free comparison of the original full production pipeline.')
        else:
            assert torch.version.cuda is None and '+cpu' in torch.__version__, 'Global CPU torch required'
        report['embedding_snapshot'] = snapshot('BAAI/bge-m3')
        report['reranker_snapshot'] = snapshot('BAAI/bge-reranker-v2-m3')
        report['missing_real_models'] = []
        queries = jsonl(benchmark / 'queries.jsonl')
        qrel_rows = jsonl(benchmark / 'qrels.jsonl')
        by_id = {q['query_id']: q for q in queries}
        assert len(by_id) == len(queries) == 60
        assert Counter(q['domain'] for q in queries) == {'apple_support': 30, 'agent_engineering': 30}
        expected = [f'{prefix}_{i:03d}' for i in range(1, 31) for prefix in ('apple', 'agent')]
        assert set(expected) == set(by_id)
        queries = [by_id[qid] for qid in expected]
        assert all(isinstance(q['query'], str) and q['query'].strip() for q in queries)
        source_paths = list(benchmark.glob('*')) + list(artifacts.rglob('*.json')) + list(artifacts.rglob('*.jsonl')) + list(artifacts.rglob('*.faiss'))
        source_paths += [ROOT / p for p in ['scripts/benchmark_reranker_backends.py', 'memory/long_term.py', 'memory/knowledge.py', 'rag/retriever.py', 'rag/dense_retriever.py', 'rag/sparse_retriever.py', 'rag/global_sparse.py', 'rag/fusion.py', 'rag/reranker.py', 'rag/build.py', 'rag/evaluation/evaluator.py', 'rag/evaluation/metrics.py', 'rag/evaluation/models.py']]
        source_hashes = {str(p.relative_to(ROOT)).replace('\\', '/'): sha(p) for p in source_paths if p.is_file()}
        report['source_hashes'] = source_hashes
        from rag.dense_retriever import load_domain_artifacts
        index_info = {}
        all_chunks = {}
        for domain in ('apple_support', 'agent_engineering'):
            a = load_domain_artifacts(artifacts, domain=domain, allow_dry_run=False)
            assert a.manifest['actual_embedding_model'] == 'BAAI/bge-m3'
            index_info[domain] = {'dimension': int(a.index.d), 'chunk_count': int(a.index.ntotal), 'model': a.manifest['actual_embedding_model'], 'backend': a.manifest['actual_embedding_backend'], 'artifact_kind': a.manifest['artifact_kind']}
            for chunk in a.chunks:
                assert chunk['chunk_id'] not in all_chunks
                all_chunks[chunk['chunk_id']] = chunk
        qrels = {qid: {} for qid in by_id}
        for row in qrel_rows:
            qid, cid = row['query_id'], row['chunk_id']
            assert qid in by_id and cid in all_chunks, f'Unknown query/chunk: {qid}/{cid}'
            assert row['domain'] == by_id[qid]['domain'] == all_chunks[cid]['domain']
            assert type(row['relevance']) is int and row['relevance'] in (1, 2)
            assert cid not in qrels[qid], f'Duplicate qrel: {qid}/{cid}'
            qrels[qid][cid] = row['relevance']
        assert all(qrels.values())
        manifest = json.loads((benchmark / 'benchmark_manifest.json').read_text(encoding='utf-8'))
        for domain, digest in manifest['source_sha256'].items():
            assert sha(artifacts / domain / 'chunks.jsonl') == digest
        report.update({'query_count': 60, 'domain_counts': dict(Counter(q['domain'] for q in queries)), 'qrel_count': len(qrel_rows), 'qrels_preserved': True, 'query_and_qrel_ids_aligned': True, 'qrel_index_chunk_ids_aligned': True, 'index_info': index_info, 'benchmark_version': manifest['benchmark_version'], 'candidate_count_by_batch': None, 'first_12_domains': [q['domain'] for q in queries[:12]]})
        report['memory_before_model_load'] = available_memory()
        # BGE-M3 weights alone occupy 2.27GB; allow model loading/activations
        # without relying on paging or reclaiming memory from live services.
        required = 6 * 1024**3
        report['minimum_available_physical_bytes_for_build'] = required
        if not CUDA_PREP and report['memory_before_model_load']['available_physical_bytes'] < required:
            raise MemoryError('Insufficient free physical RAM for safe isolated BGE-M3 CPU loading: require 6 GiB; do not terminate user services to reclaim it')
        if CUDA_PREP:
            report['cpu_full_model_load_threshold_preserved_bytes'] = required
            report['cuda_preparation_memory_policy'] = {'minimum_available_host_bytes': 1024**3, 'minimum_free_vram_bytes': 4 * 1024**3, 'loading': 'single local model, device_map cuda:0, FP32, low_cpu_mem_usage, mmap checkpoint loading; no full CPU model instance'}
            assert report['memory_before_model_load']['available_physical_bytes'] >= 1024**3, 'Insufficient RAM for mapped GPU loading overhead'
            torch.backends.cuda.matmul.allow_tf32 = False
            torch.backends.cudnn.allow_tf32 = False
            torch.set_float32_matmul_precision('highest')
        torch.set_num_threads(4)
        import memory.long_term as lt
        import rag.reranker as rr
        from scripts.benchmark_reranker_backends import build_batches
        evidence = {'encode_calls': 0, 'vector_dimensions': [], 'device': None, 'backend_class': None, 'model_name': None, 'vector_sha256': [], 'model_load_count': 0, 'backend_factory_calls': 0, 'domain_backend_identities': {}, 'minimum_available_host_bytes_observed': report['memory_before_model_load']['available_physical_bytes']}
        original_create = lt.create_embedding_backend
        shared_backend = None
        def traced_create(*args, **kwargs):
            nonlocal shared_backend
            evidence['backend_factory_calls'] += 1
            if shared_backend is not None:
                return shared_backend
            if CUDA_PREP:
                from sentence_transformers import SentenceTransformer
                backend = lt.SentenceTransformerEmbeddingBackend.__new__(lt.SentenceTransformerEmbeddingBackend)
                backend.model_name = 'BAAI/bge-m3'
                backend._model = SentenceTransformer(report['embedding_snapshot']['model_path'], device='cuda:0', local_files_only=True, model_kwargs={'device_map': {'': 'cuda:0'}, 'dtype': torch.float32, 'low_cpu_mem_usage': True})
                backend.dimension = int(backend._model.get_sentence_embedding_dimension())
                assert all(p.device.type == 'cuda' and p.dtype == torch.float32 for p in backend._model.parameters())
                evidence['parameter_count'] = sum(p.numel() for p in backend._model.parameters())
                evidence['parameter_dtype'] = 'torch.float32'
                evidence['attention_implementation'] = getattr(backend._model[0].auto_model.config, '_attn_implementation', None)
                evidence['loader'] = 'local_files_only SentenceTransformer with device_map cuda:0, low_cpu_mem_usage=True, dtype=float32; Transformers mmap bin checkpoint'
            else:
                backend = original_create(*args, **kwargs)
            evidence['model_load_count'] += 1
            shared_backend = backend
            assert isinstance(backend, lt.SentenceTransformerEmbeddingBackend)
            assert backend.model_name == 'BAAI/bge-m3' and backend.dimension == 1024
            evidence.update(backend_class=type(backend).__name__, model_name=backend.model_name, device=str(backend._model.device))
            assert evidence['device'] == ('cuda:0' if CUDA_PREP else 'cpu')
            original_embed = backend.embed_text
            def traced_embed(text):
                import numpy as np
                v = original_embed(text)
                assert v.shape == (1024,) and np.isfinite(v).all() and np.linalg.norm(v) > 0
                evidence['minimum_available_host_bytes_observed'] = min(evidence['minimum_available_host_bytes_observed'], available_memory()['available_physical_bytes'])
                if CUDA_PREP:
                    evidence['gpu_peak_allocated_bytes'] = torch.cuda.max_memory_allocated()
                    evidence['gpu_peak_reserved_bytes'] = torch.cuda.max_memory_reserved()
                evidence['encode_calls'] += 1
                evidence['vector_dimensions'].append(len(v))
                evidence['vector_sha256'].append(hashlib.sha256(v.tobytes()).hexdigest())
                return v
            backend.embed_text = traced_embed
            return backend
        # Production artifacts reject FakeReranker even when unused. This
        # non-fake fail-fast sentinel avoids loading a reranker, changes no
        # retrieval stage, and proves no scoring can accidentally happen.
        class UnusedReranker:
            def rerank(self, *args, **kwargs):
                raise AssertionError('Reranking forbidden during input preparation')
        from rag.dense_retriever import DenseRetriever
        original_dense_init = DenseRetriever.__init__
        def traced_dense_init(self, *args, **kwargs):
            original_dense_init(self, *args, **kwargs)
            evidence['domain_backend_identities'][self.domain] = id(self.embedding_backend)
        DenseRetriever.__init__ = traced_dense_init
        lt.create_embedding_backend = traced_create
        rr.create_reranker = lambda *args, **kwargs: UnusedReranker()
        def forbidden(*args, **kwargs):
            raise AssertionError('Fake/hash embedding or fake reranking forbidden')
        lt.HashEmbeddingBackend.embed_text = forbidden
        rr.FakeReranker.rerank = forbidden
        build_started = time.perf_counter()
        report['candidate_build_attempted'] = True
        raw_batches = build_batches(queries, top_k=3, pairs=20)
        report['candidate_build_seconds'] = time.perf_counter() - build_started
        assert evidence['encode_calls'] == 120  # one query per domain, no cache/semantic change
        assert evidence['model_load_count'] == 1
        assert set(evidence['domain_backend_identities']) == {'apple_support', 'agent_engineering'}
        assert len(set(evidence['domain_backend_identities'].values())) == 1
        report['memory_after_candidate_build'] = available_memory()
        batches = []
        misses = {}
        for row, raw in zip(queries, raw_batches):
            candidates = []
            for position, hit in enumerate(raw['candidates'], 1):
                text = hit.retrieval_text or hit.content
                document = rr.truncate_for_rerank(text, 768)
                assert hit.chunk_id in all_chunks and document
                candidates.append({'chunk_id': hit.chunk_id, 'document': document, 'original_chars': len(text), 'truncated_chars': len(document), 'domain': hit.domain, 'rrf_rank': position})
            assert len(candidates) == 20 and len({c['chunk_id'] for c in candidates}) == 20
            misses[row['query_id']] = sorted(set(qrels[row['query_id']]) - {c['chunk_id'] for c in candidates})
            batches.append({'query_id': row['query_id'], 'domain': row['domain'], 'query': row['query'], 'candidates': candidates, 'qrels': qrels[row['query_id']]})
        metadata = {key: report[key] for key in ('generated_at', 'torch_version', 'git_head', 'benchmark_root', 'artifact_root', 'source_hashes', 'benchmark_version', 'limitations', 'requested_configuration', 'effective_configuration')}
        metadata.update({'preparation_device': 'cuda:0 FP32' if CUDA_PREP else 'cpu', 'torch_cuda_version': report['torch_cuda_version'], 'global_cpu_torch_version': report.get('global_cpu_torch_version_previously_verified', report['torch_version']), 'memory_before_model_load': report['memory_before_model_load'], 'memory_after_candidate_build': report['memory_after_candidate_build'], 'cuda_preparation_memory_policy': report.get('cuda_preparation_memory_policy'), 'embedding_proof': evidence, 'embedding_snapshot': report['embedding_snapshot'], 'model_revision': report['reranker_snapshot']['revision'], 'environment': ENV, 'reranking_performed': False, 'real_recall_candidates_only': True, 'build_function': 'scripts.benchmark_reranker_backends.build_batches', 'build_call': {'top_k': 3, 'pairs': 20, 'calls': 1}, 'retrieval_shape': {'dense_top_k': 20, 'sparse_top_k': 20, 'domains': None, 'rrf_k': 60, 'rrf_top_k': 20}, 'unused_reranker': 'fail-fast non-fake sentinel; needed because production artifacts reject FakeReranker even when unused', 'freeze_once': True, 'scoring_mode': 'ordinary_chunk_id_graded_qrels'})
        inputs = {'schema_version': 1, 'model_id': 'BAAI/bge-reranker-v2-m3', 'model_path': report['reranker_snapshot']['model_path'], 'max_chars': 768, 'production_pairs': 9, 'quality_pairs': 20, 'metadata': metadata, 'batches': batches}
        assert all(sha(ROOT / path) == digest for path, digest in source_hashes.items()), 'Source changed during build'
        (OUT / 'inputs.json').write_text(json.dumps(inputs, ensure_ascii=False, indent=2), encoding='utf-8')
        report.update({'status': 'complete', 'inputs_generated': True, 'input_sha256': sha(OUT / 'inputs.json'), 'candidate_count_by_batch': {b['query_id']: len(b['candidates']) for b in batches}, 'embedding_proof': evidence, 'positive_qrels_not_in_top20': misses, 'positive_qrels_not_in_top20_count': sum(map(len, misses.values()))})
    except Exception as exc:
        report['blocked_reason'] = f'{type(exc).__name__}: {exc}'
        report.setdefault('candidate_build_attempted', False)
        import traceback
        traceback.print_exc()
    report['generation_seconds'] = round(time.perf_counter() - started, 3)
    (OUT / 'input_validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': report['status'], 'generation_seconds': report['generation_seconds'], 'blocked_reason': report.get('blocked_reason'), 'inputs_generated': report['inputs_generated']}, ensure_ascii=False))
    return 0 if report['status'] == 'complete' else 2

if __name__ == '__main__':
    raise SystemExit(main())
