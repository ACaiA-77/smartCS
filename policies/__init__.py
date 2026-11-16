"""Neutral business rules shared by every runtime layer.

A rule here answers "may this happen?" from deterministic evidence: which
phrases count as consent, which terms and patterns are forbidden, how PII is
masked. Both the legacy ``agents/`` path and the ``internal_api/`` path consume
these definitions, so this package must not import ``agents``, ``api``,
``internal_api``, ``context``, ``memory``, ``rag`` or ``tool_runtime`` — it is
the bottom of the dependency graph.

Not to be confused with ``context/policies.py``, which declares which context
blocks an agent receives.
"""
