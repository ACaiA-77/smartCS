"""Copilot dependency bootstrap.

Builds the connector/knowledge-engine objects the Copilot tool surface needs,
from environment variables. The rule is deliberately conservative:

* a dependency is instantiated only when ALL of its required variables are
  present, so a half-configured deployment registers no tool rather than a
  broken one;
* a partially configured mail provider is skipped, not raised on — the e-mail
  surface degrades to "not configured" while the rest still works.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from connectors.feishu import FeishuClient
from connectors.github import GitHubClient
from connectors.mail import MailRouter
from knowledge_engine import ObsidianVault, OntologyStore, WikiCompiler
from memory.long_term import (
    EmbeddingBackend,
    HashEmbeddingBackend,
    KnowledgeMemory,
    OpenAIEmbeddingBackend,
    SentenceTransformerEmbeddingBackend,
)
from rag.embeddings import BGE_M3_MODEL
from rag.reranker import (
    CrossEncoderReranker,
    FakeReranker,
    OnnxReranker,
    Reranker,
    create_reranker,
)
from rag.retriever import HybridRetriever

#: Required environment for the 163 IMAP/SMTP provider. The password alias is
#: accepted because the connector itself accepts either spelling.
_MAIL_163_KEYS = ("MAIL_163_USERNAME",)
_MAIL_163_SECRET_KEYS = ("MAIL_163_PASSWORD", "MAIL_163_AUTH_CODE")
_GMAIL_KEYS = ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")


def _env_value(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _mail_163_complete(env: Mapping[str, str]) -> bool:
    return bool(_env_value(env, "MAIL_163_USERNAME")) and any(
        _env_value(env, key) for key in _MAIL_163_SECRET_KEYS
    )


def _gmail_complete(env: Mapping[str, str]) -> bool:
    return all(_env_value(env, key) for key in _GMAIL_KEYS)


def _mail_env(env: Mapping[str, str]) -> dict[str, str]:
    """Drop the required keys of an INCOMPLETE provider before delegating.

    ``MailRouter.from_env`` intentionally refuses partial credentials. Bootstrap
    is a different contract: an unrelated half-filled provider must not stop the
    complete one from loading, so its triggering variables are removed and the
    router only ever sees whole provider families.
    """

    subset = dict(env)
    if not _mail_163_complete(env):
        for key in (*_MAIL_163_KEYS, *_MAIL_163_SECRET_KEYS):
            subset.pop(key, None)
    if not _gmail_complete(env):
        for key in _GMAIL_KEYS:
            subset.pop(key, None)
    return subset


def _build_mail_router(env: Mapping[str, str]) -> MailRouter | None:
    if not (_mail_163_complete(env) or _gmail_complete(env)):
        return None
    return MailRouter.from_env(_mail_env(env))


_LOCAL_EMBEDDING_BACKENDS = frozenset({"hash", "fake", "local", "sentence_transformers", "auto"})
_REMOTE_BACKEND_MARKERS = ("openai", "remote", "http", "cohere", "voyage", "azure", "anthropic", "bedrock")
_LOCAL_RERANKER_BACKENDS = frozenset(
    {"fake", "hash", "dry_run", "cross_encoder", "sentence_transformers", "local", "onnx_int8", "onnx"}
)


def is_local_embedding_backend(backend: Any) -> bool:
    """Return True only when ``backend`` keeps text local (never sends notes over HTTP)."""
    if backend is None or isinstance(backend, OpenAIEmbeddingBackend):
        return False
    if isinstance(backend, (HashEmbeddingBackend, SentenceTransformerEmbeddingBackend)):
        return True
    name = str(getattr(backend, "backend_name", "") or backend.__class__.__name__).strip().lower()
    return not any(marker in name for marker in _REMOTE_BACKEND_MARKERS)


def is_local_reranker(reranker: Any) -> bool:
    """Return True only for offline/in-process rerankers."""
    if reranker is None:
        return False
    if isinstance(reranker, (FakeReranker, CrossEncoderReranker, OnnxReranker)):
        return True
    name = str(getattr(reranker, "backend_name", "") or reranker.__class__.__name__).strip().lower()
    return name in _LOCAL_RERANKER_BACKENDS


def resolve_local_obsidian_embedding_backend(
    env: Mapping[str, str] | None = None,
    *,
    backend: EmbeddingBackend | str | None = None,
    model_name: str | None = None,
    embedding_dim: int = 1536,
) -> EmbeddingBackend:
    """Build or validate a strictly local embedding backend for private Obsidian notes."""
    values = os.environ if env is None else env
    if backend is not None and not isinstance(backend, str):
        if not is_local_embedding_backend(backend):
            raise ValueError(
                "private Obsidian index requires a local embedding backend; remote/openai backend is forbidden"
            )
        return backend

    raw_backend = backend if isinstance(backend, str) else (_env_value(values, "EMBEDDING_BACKEND") or "hash")
    selected = raw_backend.strip().lower()
    if selected in {"openai", "remote"} or selected not in _LOCAL_EMBEDDING_BACKENDS:
        raise ValueError(
            f"private Obsidian index requires a local embedding backend, got {raw_backend!r}"
        )

    resolved_model = model_name or _env_value(values, "EMBEDDING_MODEL") or BGE_M3_MODEL
    if selected in {"local", "sentence_transformers"}:
        return SentenceTransformerEmbeddingBackend(resolved_model)
    if selected == "auto":
        try:
            return SentenceTransformerEmbeddingBackend(resolved_model)
        except Exception:
            return HashEmbeddingBackend(embedding_dim)
    return HashEmbeddingBackend(embedding_dim)


def resolve_local_obsidian_reranker(
    env: Mapping[str, str] | None = None,
    *,
    reranker: Reranker | str | None = None,
) -> Reranker:
    """Build or validate a strictly local reranker for private Obsidian notes."""
    values = os.environ if env is None else env
    if reranker is not None and not isinstance(reranker, str):
        if not is_local_reranker(reranker):
            raise ValueError("private Obsidian retriever requires a local reranker")
        return reranker

    raw_backend = reranker if isinstance(reranker, str) else (_env_value(values, "RAG_RERANKER_BACKEND") or "fake")
    selected = raw_backend.strip().lower()
    if selected not in _LOCAL_RERANKER_BACKENDS:
        raise ValueError(f"private Obsidian retriever requires a local reranker, got {raw_backend!r}")
    return create_reranker(selected)


def build_copilot_dependencies(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Instantiate every configured Copilot dependency; unconfigured ones are None.

    The returned mapping is shaped for ``tool_runtime.mcp_server.create_default_tools``
    (``vault``, ``ontology``, ``wiki_compiler``, ``mail_router``, ``github_client``,
    ``feishu_client``), so the caller can splat it directly.
    """

    values = os.environ if env is None else env

    vault = None
    vault_path = _env_value(values, "OBSIDIAN_VAULT_PATH")
    source_vault_path = _env_value(values, "OBSIDIAN_SOURCE_VAULT_PATH")
    if vault_path:
        vault = ObsidianVault(vault_path, source_root=source_vault_path)
    elif source_vault_path:
        vault = ObsidianVault(source_vault_path, read_only=True)

    ontology = None
    ontology_path = _env_value(values, "COPILOT_ONTOLOGY_DB_PATH")
    if ontology_path:
        ontology = OntologyStore(ontology_path)

    wiki_compiler = WikiCompiler(vault, ontology) if vault is not None and not vault.read_only else None

    github_token = _env_value(values, "GITHUB_TOKEN")
    github_client = GitHubClient(github_token) if github_token else None

    feishu_app_id = _env_value(values, "FEISHU_APP_ID")
    feishu_app_secret = _env_value(values, "FEISHU_APP_SECRET")
    feishu_client = (
        FeishuClient(feishu_app_id, feishu_app_secret)
        if feishu_app_id and feishu_app_secret
        else None
    )

    return {
        "vault": vault,
        "ontology": ontology,
        "wiki_compiler": wiki_compiler,
        "mail_router": _build_mail_router(values),
        "github_client": github_client,
        "feishu_client": feishu_client,
    }


def assert_isolated_obsidian_index_path(
    index_path: str | Path,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Ensure ``OBSIDIAN_INDEX_PATH`` stays separate from shared FAISS and RAG artifacts."""

    values = os.environ if env is None else env
    resolved = Path(index_path).expanduser().resolve()
    shared_faiss = Path(_env_value(values, "FAISS_INDEX_PATH") or "./vector_store/faiss_index").expanduser().resolve()
    if resolved == shared_faiss or resolved.is_relative_to(shared_faiss):
        raise ValueError("OBSIDIAN_INDEX_PATH must stay isolated from FAISS_INDEX_PATH")
    rag_root_raw = _env_value(values, "RAG_INDEX_ROOT")
    if rag_root_raw:
        rag_root = Path(rag_root_raw).expanduser().resolve()
        if resolved == rag_root or resolved.is_relative_to(rag_root):
            raise ValueError("OBSIDIAN_INDEX_PATH must stay isolated from RAG_INDEX_ROOT")
    return resolved


def obsidian_rag_document_count(obsidian_retriever: Any) -> int:
    """Return the number of indexed personal Obsidian chunks."""

    if obsidian_retriever is None:
        return 0
    return len(getattr(getattr(obsidian_retriever, "legacy_memory", None), "documents", []) or [])


def build_obsidian_retriever(
    env: Mapping[str, str] | None = None,
    *,
    embedding_backend: EmbeddingBackend | None = None,
    reranker: Reranker | None = None,
) -> HybridRetriever | None:
    """Build the isolated, local-only RAG retriever for ``OBSIDIAN_INDEX_PATH``.

    Never shares indexes with ``FAISS_INDEX_PATH`` or ``RAG_INDEX_ROOT``, and
    refuses remote embedding/reranker backends so private notes never leave the
    host or enter the shared MCP gateway.
    """

    values = os.environ if env is None else env
    index_raw = _env_value(values, "OBSIDIAN_INDEX_PATH")
    if not index_raw:
        return None
    index_path = assert_isolated_obsidian_index_path(index_raw, values)
    metadata_path = index_path.with_suffix(".meta.json")
    if not index_path.is_file() or not metadata_path.is_file():
        return None

    local_embedding = resolve_local_obsidian_embedding_backend(values, backend=embedding_backend)
    local_reranker = resolve_local_obsidian_reranker(values, reranker=reranker)
    memory = KnowledgeMemory(
        index_path=str(index_path),
        embedding_backend=local_embedding,
    )
    if not memory.documents:
        return None
    # Explicitly construct with artifact_root=None and legacy_memory=memory so
    # process-wide RAG_INDEX_ROOT never hijacks personal Obsidian retrieval.
    return HybridRetriever(
        None,
        embedding_backend=local_embedding,
        reranker=local_reranker,
        legacy_memory=memory,
    )


__all__ = [
    "assert_isolated_obsidian_index_path",
    "build_copilot_dependencies",
    "build_obsidian_retriever",
    "is_local_embedding_backend",
    "is_local_reranker",
    "obsidian_rag_document_count",
    "resolve_local_obsidian_embedding_backend",
    "resolve_local_obsidian_reranker",
]
