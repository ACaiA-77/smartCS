"""Explicit, opt-in BM25 candidate using one corpus across both domains."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from .build import DOMAINS, _terms
from .dense_retriever import ArtifactValidationError, load_domain_artifacts
from .models import RetrievalHit


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def build_global_sparse_artifact(
    artifact_root: str | Path, *, output_dir: str | Path | None = None,
    allow_dry_run: bool = False,
) -> Path:
    """Freeze merged BM25 statistics without rechunking or retokenizing sources."""
    root = Path(artifact_root)
    target = Path(output_dir) if output_dir is not None else root / "global_sparse"
    if target.resolve() in {(root / domain).resolve() for domain in DOMAINS}:
        raise ValueError("global sparse output overlaps a domain artifact")
    sources = {domain: load_domain_artifacts(root, domain=domain, allow_dry_run=allow_dry_run)
               for domain in DOMAINS}
    if len({source.manifest["chunking_version"] for source in sources.values()}) != 1:
        raise ArtifactValidationError("global sparse sources use different chunking versions")
    if len({(source.bm25["k1"], source.bm25["b"]) for source in sources.values()}) != 1:
        raise ArtifactValidationError("global sparse sources use different k1/b")
    chunk_ids, document_domains, lengths, frequencies = [], [], [], []
    postings: dict[str, list[dict[str, int]]] = defaultdict(list)
    for domain in DOMAINS:
        source = sources[domain]
        offset = len(chunk_ids)
        local = source.bm25
        chunk_ids.extend(local["chunk_ids"])
        document_domains.extend([domain] * len(local["chunk_ids"]))
        lengths.extend(local["document_lengths"])
        frequencies.extend(local["term_frequencies"])
        for term, entries in local["postings"].items():
            postings[term].extend({"index": offset + entry["index"], "tf": entry["tf"]}
                                  for entry in entries)
    if len(set(chunk_ids)) != len(chunk_ids):
        raise ArtifactValidationError("global sparse chunk IDs overlap across domains")
    index = {
        "version": "global-bm25-v1", "k1": sources[DOMAINS[0]].bm25["k1"],
        "b": sources[DOMAINS[0]].bm25["b"], "domains": list(DOMAINS),
        "chunk_ids": chunk_ids, "document_domains": document_domains,
        "document_lengths": lengths, "term_frequencies": frequencies,
        "document_frequency": {term: len(entries) for term, entries in postings.items()},
        "average_document_length": sum(lengths) / len(lengths),
        "postings": dict(postings),
    }
    index_bytes = _bytes(index)
    manifest = {
        "schema_version": 1, "artifact_kind": "global_sparse_candidate",
        "domains": list(DOMAINS), "k1": index["k1"], "b": index["b"],
        "document_count": len(chunk_ids),
        "average_document_length": index["average_document_length"],
        "chunking_version": sources[DOMAINS[0]].manifest["chunking_version"],
        "tokenization_build_provenance": "frozen domain BM25 term frequencies/postings; original tokenizer backend not recorded",
        "query_tokenizer": "rag.build._terms at retrieval runtime",
        "bm25_index_sha256": hashlib.sha256(index_bytes).hexdigest(),
        "sources": {domain: {name: _hash(root / domain / name) for name in
                             ("manifest.json", "chunks.jsonl", "bm25_index.json")}
                    for domain in DOMAINS},
    }
    target.mkdir(parents=True, exist_ok=True)
    for name, data in (("bm25_index.json", index_bytes), ("manifest.json", _bytes(manifest))):
        path = target / name
        if path.exists() and path.read_bytes() != data:
            raise ArtifactValidationError(f"existing global sparse artifact differs: {path}")
        if not path.exists():
            path.write_bytes(data)
    return target


class GlobalSparseRetriever:
    """Load the frozen global artifact; fail closed if any source has drifted."""

    def __init__(self, artifact_root: str | Path, *, allow_dry_run: bool = False) -> None:
        root = Path(artifact_root)
        directory = root / "global_sparse"
        try:
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            index_path = directory / "bm25_index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise ArtifactValidationError("missing or invalid global sparse artifact") from exc
        if (manifest.get("schema_version") != 1 or manifest.get("artifact_kind") != "global_sparse_candidate"
                or manifest.get("domains") != list(DOMAINS) or index.get("domains") != list(DOMAINS)
                or index.get("version") != "global-bm25-v1"
                or _hash(index_path) != manifest.get("bm25_index_sha256")):
            raise ArtifactValidationError("global sparse manifest/index mismatch")
        chunks = []
        for domain in DOMAINS:
            expected = manifest.get("sources", {}).get(domain, {})
            if any(_hash(root / domain / name) != expected.get(name) for name in
                   ("manifest.json", "chunks.jsonl", "bm25_index.json")):
                raise ArtifactValidationError(f"global sparse source changed: {domain}")
            source = load_domain_artifacts(root, domain=domain, allow_dry_run=allow_dry_run)
            if source.manifest["chunking_version"] != manifest.get("chunking_version"):
                raise ArtifactValidationError("global sparse chunking version mismatch")
            chunks.extend(source.chunks)
        n = len(chunks)
        if (not n or index.get("chunk_ids") != [chunk["chunk_id"] for chunk in chunks]
                or index.get("document_domains") != [chunk["domain"] for chunk in chunks]
                or manifest.get("document_count") != n
                or len(index.get("document_lengths", [])) != n
                or len(index.get("term_frequencies", [])) != n
                or index.get("k1") != manifest.get("k1") or index.get("b") != manifest.get("b")
                or index.get("average_document_length") != manifest.get("average_document_length")
                or abs(sum(index["document_lengths"]) / n - index["average_document_length"]) > 1e-12):
            raise ArtifactValidationError("global sparse document coverage/statistics mismatch")
        postings, df = index.get("postings"), index.get("document_frequency")
        if not isinstance(postings, dict) or not isinstance(df, dict) or set(postings) != set(df):
            raise ArtifactValidationError("global sparse postings/df mismatch")
        if any(sum(frequencies.values()) != length for frequencies, length in
               zip(index["term_frequencies"], index["document_lengths"])):
            raise ArtifactValidationError("global sparse document length/TF mismatch")
        if dict(Counter(term for frequencies in index["term_frequencies"] for term in frequencies)) != df:
            raise ArtifactValidationError("global sparse document frequencies mismatch")
        for term, entries in postings.items():
            if df[term] != len(entries) or len({entry["index"] for entry in entries}) != len(entries):
                raise ArtifactValidationError(f"global sparse posting count mismatch: {term}")
            for entry in entries:
                position, tf = entry["index"], entry["tf"]
                if not isinstance(position, int) or position < 0 or position >= n or index["term_frequencies"][position].get(term) != tf:
                    raise ArtifactValidationError(f"global sparse posting TF mismatch: {term}")
        self.index = index
        self.chunks = chunks

    def search(self, query: str, top_k: int = 5) -> list[RetrievalHit]:
        if not str(query).strip() or top_k <= 0:
            return []
        index = self.index
        scores: dict[int, float] = defaultdict(float)
        n = len(self.chunks)
        for term in sorted(set(_terms(query))):
            df = index["document_frequency"].get(term, 0)
            if not df:
                continue
            idf = math.log((n - df + 0.5) / (df + 0.5) + 1.0)
            for entry in index["postings"][term]:
                position, tf = entry["index"], entry["tf"]
                norm = 1 - index["b"] + index["b"] * index["document_lengths"][position] / index["average_document_length"]
                scores[position] += idf * tf * (index["k1"] + 1) / (tf + index["k1"] * norm)
        ranked = sorted(
            ((position, score) for position, score in scores.items() if score > 0),
            key=lambda row: (-row[1], self.chunks[row[0]]["domain"], self.chunks[row[0]]["chunk_id"]),
        )[:top_k]
        return [RetrievalHit(
            chunk_id=chunk["chunk_id"], domain=chunk["domain"], rank=rank,
            score=float(score), source=chunk.get("source", ""), content=chunk.get("content", ""),
            retrieval_text=chunk.get("retrieval_text") or chunk.get("content", ""),
            heading_path=list(chunk.get("heading_path") or []), metadata=dict(chunk),
            sparse_rank=rank, sparse_score=float(score),
        ) for rank, (position, score) in enumerate(ranked, 1)
           for chunk in (self.chunks[position],)]
