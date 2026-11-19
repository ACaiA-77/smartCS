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
from typing import Any

from connectors.feishu import FeishuClient
from connectors.github import GitHubClient
from connectors.mail import MailRouter
from knowledge_engine import ObsidianVault, OntologyStore, WikiCompiler

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


def build_copilot_dependencies(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Instantiate every configured Copilot dependency; unconfigured ones are None.

    The returned mapping is shaped for ``tool_runtime.mcp_server.create_default_tools``
    (``vault``, ``ontology``, ``wiki_compiler``, ``mail_router``, ``github_client``,
    ``feishu_client``), so the caller can splat it directly.
    """

    values = os.environ if env is None else env

    vault = None
    vault_path = _env_value(values, "OBSIDIAN_VAULT_PATH")
    if vault_path:
        vault = ObsidianVault(vault_path)

    ontology = None
    ontology_path = _env_value(values, "COPILOT_ONTOLOGY_DB_PATH")
    if ontology_path:
        ontology = OntologyStore(ontology_path)

    wiki_compiler = WikiCompiler(vault, ontology) if vault is not None else None

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


__all__ = ["build_copilot_dependencies"]
