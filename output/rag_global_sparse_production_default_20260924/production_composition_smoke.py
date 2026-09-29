"""Check real API composition and sparse artifact selection without starting a server."""

import os
from pathlib import Path


os.environ["EMBEDDING_BACKEND"] = "local"
os.environ["EMBEDDING_MODEL"] = "BAAI/bge-m3"
os.environ["RAG_INDEX_ROOT"] = str(Path("artifacts/rag_round3/production_indexes").resolve())
os.environ["RAG_RERANKER_BACKEND"] = "cross_encoder"
os.environ.pop("RAG_SPARSE_MODE", None)

from api import main as api  # noqa: E402


retriever = api.shared_retriever
assert retriever.sparse_mode == "global_corpus_v1" and retriever.is_artifact_mode
assert retriever.sparse_search("AppleCare", top_k=1)
assert retriever.sparse_search("AppleCare", domains=["apple_support"], top_k=1)
os.environ["RAG_SPARSE_MODE"] = "domain_local_v1"
rollback = api.long_term_memory.get_retriever()
assert rollback is not retriever and rollback.sparse_mode == "domain_local_v1"
print("PASS: API production composition uses global sparse; explicit rollback and single-domain path work")
