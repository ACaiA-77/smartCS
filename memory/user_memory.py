"""Owner-scoped, source-bound profile and episodic memory.

``process_message`` is an after-commit enqueue operation; it never applies a
candidate inline. A bounded worker calls ``process_pending``/``drain_pending``.
Production storage is MySQL through ``PlatformDatabase._call``. The in-memory
repository is an explicitly injected deterministic test double only.

Profile cards are stable user preferences: clearing a conversation hides that
conversation's episodes, but does not erase a profile preference. Preferences
remain until superseded or explicitly deactivated with ``deactivate_profile``.
Episodes are always historical, source-linked, and non-authoritative; every
source in a consolidated abstraction must remain beyond its session clear
cutoff to be recalled.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Protocol

from platform_db.database import PlatformDatabase


class MemoryProvenanceError(PermissionError):
    """A memory write did not reference the exact owned user-message event."""


class MemoryDecision(StrEnum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    MERGE = "MERGE"
    IGNORE = "IGNORE"


class CandidateKind(StrEnum):
    PROFILE = "profile"
    EPISODE = "episode"


@dataclass(frozen=True)
class MemoryCandidate:
    kind: CandidateKind
    user_id: str
    category: str
    key: str
    value_json: dict[str, Any]
    confidence: float
    source_session_id: str
    source_event_id: str
    sensitivity: str = "normal"
    reason: str = "explicit_user_statement"
    eligible: bool = True
    source_seq: int | None = None
    source_created_at: datetime | str | None = None

    @property
    def candidate_key(self) -> str:
        material = {
            "kind": self.kind.value,
            "category": self.category,
            "key": self.key,
            "value_json": self.value_json,
            "eligible": self.eligible,
            "reason": self.reason,
            "sensitivity": self.sensitivity,
        }
        return hashlib.sha256(_json_dumps(material).encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class PendingCandidate:
    candidate_id: str
    candidate: MemoryCandidate
    claim_token: str
    attempts: int = 0


class UserMemoryRepository(Protocol):
    async def initialize(self) -> None: ...
    async def source_event(self, user_id: str, session_id: str, event_id: str) -> dict[str, Any] | None: ...
    async def profile_cards(self, user_id: str, limit: int = 3) -> list[dict[str, Any]]: ...
    async def active_card(self, user_id: str, category: str, key: str) -> dict[str, Any] | None: ...
    async def record_candidate(self, candidate: MemoryCandidate) -> tuple[str, bool]: ...
    async def claim_pending(self, user_id: str, limit: int, lease_seconds: int = 60) -> list[PendingCandidate]: ...
    async def pending_user_ids(self, limit: int = 50) -> list[str]: ...
    async def mark_candidate(self, candidate_id: str, decision: MemoryDecision, reason: str, claim_token: str) -> None: ...
    async def release_candidate(self, candidate_id: str, claim_token: str, error_type: str) -> None: ...
    async def apply_profile_candidate(self, candidate: MemoryCandidate) -> tuple[MemoryDecision, str]: ...
    async def add_episode(self, candidate: MemoryCandidate) -> bool: ...
    async def episodes(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]: ...
    async def search_episodes(self, user_id: str, query_terms: list[str], limit: int = 500) -> list[dict[str, Any]]: ...
    async def consolidate(self, user_id: str) -> dict[str, Any]: ...
    async def deactivate_profile(self, user_id: str, category: str, key: str) -> bool: ...


class MemoryExtractor:
    """Deterministic extraction of only explicit, stable user preferences.

    Business-state mentions become compact historical episodes, never profile
    facts. Unrecognized preference wording is ignored rather than inferred.
    """

    _sensitive_re = re.compile(
        r"(password|passcode|secret|api[_ -]?key|access token|bearer token|"
        r"cvv|cvc|ssn|social security|credit card|debit card|card number|"
        r"bank account|routing number|iban|身份证|银行卡|信用卡|借记卡|"
        r"支付密码|密码|验证码|安全码|卡号|银行卡号)",
        re.IGNORECASE,
    )
    _card_number_re = re.compile(r"(?<!\d)(?:\d[ -]*?){13,19}(?!\d)")
    _business_state_re = re.compile(
        r"(order|refund|payment|paid|invoice|delivery|shipment|account status|balance|"
        r"订单|退款|支付|付款|发票|物流|账户状态|余额)",
        re.IGNORECASE,
    )
    _language_patterns = (
        ("zh-CN", re.compile(
            r"(?:reply|respond|answer|speak|write)\s+(?:to me\s+)?in\s+(?:simplified\s+)?(?:chinese|mandarin)|"
            r"(?:i prefer|i'd prefer|my preferred language is)\s+(?:simplified\s+)?(?:chinese|mandarin)|"
            r"\buse (?:simplified )?chinese\b|"
            r"(?:以后)?(?:请)?用(?:简体)?中文(?:回复|回答|交流)?|"
            r"(?:简体)?中文(?:回复|回答|交流)|"
            r"我更喜欢(?:用)?(?:简体)?中文|我的(?:回复)?语言(?:偏好)?是(?:简体)?中文",
            re.I,
        )),
        ("en", re.compile(r"(?:reply|respond|answer|speak|write)\s+(?:to me\s+)?in\s+english|(?:i prefer|i'd prefer|my preferred language is)\s+english|\buse english\b|用英文(?:回复|回答|交流)?|以后(?:请)?用英文|英文回复|我更喜欢英文", re.I)),
    )
    _historical_id_patterns = {
        "order_ids": re.compile(r"\b(?:ORD|ORDER)(?:[-_#:][A-Z0-9][A-Z0-9_-]*|[0-9][A-Z0-9_-]*)\b", re.I),
        "ticket_ids": re.compile(r"\b(?:TKT|TICKET|CASE|CS)(?:[-_#:][A-Z0-9][A-Z0-9_-]*|[0-9][A-Z0-9_-]*)\b", re.I),
        "invoice_ids": re.compile(r"\b(?:INV|INVOICE)(?:[-_#:][A-Z0-9][A-Z0-9_-]*|[0-9][A-Z0-9_-]*)\b", re.I),
        "payment_ids": re.compile(r"\b(?:PAY|PAYMENT)(?:[-_#:][A-Z0-9][A-Z0-9_-]*|[0-9][A-Z0-9_-]*)\b", re.I),
        "refund_ids": re.compile(r"\b(?:RFD|REFUND)(?:[-_#:][A-Z0-9][A-Z0-9_-]*|[0-9][A-Z0-9_-]*)\b", re.I),
    }
    _amount_re = re.compile(
        r"(?<![\w])(?:[$€£¥]\s*\d[\d,]*(?:\.\d{1,2})?|"
        r"\d[\d,]*(?:\.\d{1,2})?\s?(?:USD|EUR|GBP|CAD|AUD|CNY|RMB)\b)", re.I,
    )
    _date_re = re.compile(
        r"\b(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4}|"
        r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"\s+\d{1,2}(?:,?\s+\d{4})?)\b", re.I,
    )

    def extract(self, user_id: str, session_id: str, event_id: str, content: str) -> list[MemoryCandidate]:
        text = (content or "").strip()
        if not text:
            return []
        if self._contains_sensitive(text):
            return [
                self._candidate(
                    user_id, session_id, event_id, CandidateKind.PROFILE,
                    "security", "rejected_sensitive_candidate",
                    {"rejected": True, "classification": "credential_or_payment_personal_detail"},
                    0.0, sensitivity="restricted", reason="sensitive_candidate_rejected", eligible=False,
                )
            ]

        candidates: list[MemoryCandidate] = []
        for language, pattern in self._language_patterns:
            if pattern.search(text):
                candidates.append(self._candidate(
                    user_id, session_id, event_id, CandidateKind.PROFILE,
                    "preference", "response_language",
                    {"language": language, "label": "Chinese" if language == "zh-CN" else "English", "source": "explicit_preference"},
                    0.9, sensitivity="normal",
                ))
                break
        if re.search(r"(be concise|keep (?:it|the answer|your answers) (?:concise|brief|short)|"
                     r"i prefer (?:a )?(?:concise|brief) (?:answer|response|style)|"
                     r"respond concisely|简洁一点|请简洁|回答简洁|回复简短|尽量简短|短一点)", text, re.I):
            candidates.append(self._candidate(
                user_id, session_id, event_id, CandidateKind.PROFILE,
                "preference", "response_style", {"style": "concise", "source": "explicit_preference"},
                0.85, sensitivity="normal",
            ))
        elif re.search(r"(be detailed|give me (?:a )?detailed|keep (?:it|the answer) detailed|"
                       r"i prefer (?:a )?detailed (?:answer|response)|step.by.step|详细一点|请详细|展开说|一步一步)", text, re.I):
            candidates.append(self._candidate(
                user_id, session_id, event_id, CandidateKind.PROFILE,
                "preference", "response_style", {"style": "detailed", "source": "explicit_preference"},
                0.85, sensitivity="normal",
            ))
        if re.search(r"(use a professional tone|be professional|i prefer a formal tone|"
                     r"正式一点|请用正式语气|专业一点)", text, re.I):
            candidates.append(self._candidate(
                user_id, session_id, event_id, CandidateKind.PROFILE,
                "preference", "tone", {"tone": "professional", "source": "explicit_preference"},
                0.8, sensitivity="normal",
            ))

        preferred_name = self._preferred_name(text)
        if preferred_name:
            candidates.append(self._candidate(
                user_id, session_id, event_id, CandidateKind.PROFILE,
                "identity", "preferred_name", {"name": preferred_name, "source": "explicit_preference"},
                0.75, sensitivity="personal",
            ))

        if self._business_state_re.search(text):
            topics = self._business_topics(text)
            labels = {
                "order": "order/订单", "refund": "refund/退款",
                "payment": "payment/支付", "account_status": "account status/账户状态",
            }
            historical_refs = self._historical_references(text)
            quote, quote_truncated = _bounded_historical_quote(
                text, max_chars=1200, identifier_patterns=tuple(self._historical_id_patterns.values())
            )
            topic_text = ", ".join(labels[item] for item in topics)
            candidates.append(self._candidate(
                user_id, session_id, event_id, CandidateKind.EPISODE,
                "episode", "business_state_mention",
                {
                    "summary": f"Historical user statement about {topic_text}; source quote: {quote}",
                    "historical_quote": quote,
                    "quote_truncated": quote_truncated,
                    "structured_refs": historical_refs,
                    "topics": topics,
                    "historical": True,
                    "not_authoritative": True,
                    "requires_live_tool_lookup": True,
                    "source_readonly_hint": "Historical user-statement evidence only; inspect the cited event, then use a live authorized tool for current state.",
                },
                0.55, sensitivity="normal", reason="business_state_episode_only",
            ))
        return candidates

    def _contains_sensitive(self, text: str) -> bool:
        return bool(self._sensitive_re.search(text) or self._card_number_re.search(text))

    def _preferred_name(self, text: str) -> str | None:
        patterns = (
            r"\bcall me\s+([A-Za-z][A-Za-z0-9 _.-]{0,39})\b",
            r"\bmy name is\s+([A-Za-z][A-Za-z0-9 _.-]{0,39})\b",
            r"(?:叫我|称呼我为|我叫)\s*([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9 _.-]{0,19})",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue
            name = match.group(1).strip()
            name = re.split(r"[,，.!?。！？;；\n]", name)[0].strip()
            name = re.split(
                r"\s+(?:and|but)\s+(?=(?:please\s+)?(?:reply|respond|answer|speak|write|use|keep|be)\b)|"
                r"(?:以后请|请用(?:简体)?中文|用(?:简体)?中文|请回复|请回答|并请用)",
                name, maxsplit=1, flags=re.IGNORECASE,
            )[0].strip()
            if name and not self._business_state_re.search(name) and not self._contains_sensitive(name):
                return name[:40]
        return None

    def _historical_references(self, text: str) -> dict[str, list[str]]:
        references = {
            field: _unique_text_matches(pattern.findall(text))
            for field, pattern in self._historical_id_patterns.items()
        }
        references["amounts"] = _unique_text_matches(self._amount_re.findall(text))
        references["dates"] = _unique_text_matches(self._date_re.findall(text))
        references["identifiers"] = _unique_text_matches(
            identifier for field, values in references.items() if field.endswith("_ids")
            for identifier in values
        )
        return references

    def _business_topics(self, text: str) -> list[str]:
        mapping = {
            "order": r"order|订单|物流|delivery|shipment",
            "refund": r"refund|退款",
            "payment": r"payment|paid|支付|付款",
            "account_status": r"account status|balance|账户状态|余额",
        }
        return [topic for topic, pattern in mapping.items() if re.search(pattern, text, re.I)] or ["business_state"]

    @staticmethod
    def _candidate(
        user_id: str, session_id: str, event_id: str, kind: CandidateKind,
        category: str, key: str, value_json: dict[str, Any], confidence: float,
        *, sensitivity: str, reason: str = "explicit_user_statement", eligible: bool = True,
    ) -> MemoryCandidate:
        return MemoryCandidate(
            kind=kind, user_id=user_id, category=category, key=key,
            value_json=value_json, confidence=max(0.0, min(float(confidence), 1.0)),
            source_session_id=session_id, source_event_id=event_id,
            sensitivity=sensitivity, reason=reason, eligible=eligible,
        )


class MemoryPolicy:
    """Central deterministic decision policy for profile and episode writes."""

    _PROFILE_KEYS = {
        ("preference", "response_language"),
        ("preference", "response_style"),
        ("preference", "tone"),
        ("identity", "preferred_name"),
    }

    def profile_valid(self, candidate: MemoryCandidate) -> bool:
        if candidate.kind != CandidateKind.PROFILE or (candidate.category, candidate.key) not in self._PROFILE_KEYS:
            return False
        value = candidate.value_json
        allowed_fields = {
            "response_language": {"language", "label", "source"},
            "response_style": {"style", "source"},
            "tone": {"tone", "source"},
            "preferred_name": {"name", "source"},
        }
        if (
            not isinstance(value, dict) or not _is_strict_json_value(value)
            or set(value) - allowed_fields[candidate.key]
            or any(not isinstance(item, str) for item in value.values())
            or type(candidate.confidence) not in (int, float)
            or not math.isfinite(candidate.confidence) or not 0 <= candidate.confidence <= 1
            or type(candidate.eligible) is not bool
            or candidate.sensitivity not in {"normal", "personal"}
        ):
            return False
        if candidate.key == "response_language":
            return value.get("language") in {"zh-CN", "en"}
        if candidate.key == "response_style":
            return value.get("style") in {"concise", "detailed"}
        if candidate.key == "tone":
            return value.get("tone") == "professional"
        name = value.get("name")
        return isinstance(name, str) and 1 <= len(name) <= 40 and not MemoryExtractor()._contains_sensitive(name)

    def episode_valid(self, candidate: MemoryCandidate) -> bool:
        value = candidate.value_json
        if (
            candidate.kind != CandidateKind.EPISODE
            or candidate.category != "episode"
            or candidate.key != "business_state_mention"
            or not candidate.eligible
            or candidate.sensitivity == "restricted"
            or type(candidate.confidence) not in (int, float)
            or not math.isfinite(float(candidate.confidence))
            or not 0.0 <= float(candidate.confidence) <= 1.0
            or not isinstance(candidate.user_id, str) or not candidate.user_id.strip()
            or not isinstance(candidate.source_session_id, str) or not candidate.source_session_id.strip()
            or not isinstance(candidate.source_event_id, str)
            or not candidate.source_event_id.isdecimal() or int(candidate.source_event_id) <= 0
            or type(candidate.source_seq) is not int or candidate.source_seq <= 0
            or _parse_dt(candidate.source_created_at) is None
            or not isinstance(value, dict) or not _is_strict_json_value(value)
            or value.get("historical") is not True
            or value.get("not_authoritative") is not True
            or value.get("requires_live_tool_lookup") is not True
            or not isinstance(value.get("summary"), str) or not value["summary"].strip()
            or not isinstance(value.get("historical_quote"), str)
            or not value["historical_quote"].strip() or len(value["historical_quote"]) > 1200
            or type(value.get("quote_truncated")) is not bool
            or MemoryExtractor()._contains_sensitive(_json_dumps(value))
        ):
            return False
        topics = value.get("topics")
        if not isinstance(topics, list) or not topics or any(
            not isinstance(topic, str) or topic not in {
                "order", "refund", "payment", "account_status", "business_state"
            } for topic in topics
        ):
            return False
        refs = value.get("structured_refs", {})
        if not isinstance(refs, dict) or any(
            not isinstance(items, list) or any(not isinstance(item, str) for item in items)
            for items in refs.values()
        ):
            return False
        return True

    def decide_profile(
        self,
        candidate: MemoryCandidate,
        active: dict[str, Any] | None,
        latest_inactive: dict[str, Any] | None = None,
    ) -> tuple[MemoryDecision, str]:
        if not candidate.eligible or candidate.sensitivity == "restricted":
            return MemoryDecision.IGNORE, candidate.reason
        if not self.profile_valid(candidate):
            return MemoryDecision.IGNORE, "profile_policy_rejected"
        if _parse_dt(candidate.source_created_at) is None:
            return MemoryDecision.IGNORE, "missing_source_timestamp"
        if active is None:
            if latest_inactive is not None:
                cutoff = latest_inactive.get("valid_to") or latest_inactive.get("updated_at")
                if cutoff and _source_order(candidate.source_created_at, candidate.source_event_id) <= _source_order(cutoff, latest_inactive.get("source_event_id")):
                    return MemoryDecision.IGNORE, "older_than_deactivation_or_latest_card"
            return MemoryDecision.ADD, candidate.reason
        same_value = _canonical_json(active.get("value_json", {})) == _canonical_json(candidate.value_json)
        order = _source_order(candidate.source_created_at, candidate.source_event_id)
        active_order = _source_order(
            active.get("source_created_at") or active.get("valid_from"),
            active.get("source_event_id"),
        )
        if same_value:
            return MemoryDecision.MERGE, "same_preference_merged"
        if order <= active_order:
            return MemoryDecision.IGNORE, "older_source_cannot_override_newer_preference"
        return MemoryDecision.UPDATE, "newer_explicit_preference_supersedes_active_version"


class MySQLUserMemoryRepository:
    """Production repository. Every query is owner-scoped via PlatformDatabase."""

    def __init__(self, db: PlatformDatabase):
        self.db = db

    async def initialize(self) -> None:
        def create(_connection, cursor):
            cursor.execute(
                """CREATE TABLE IF NOT EXISTS user_memory_profile_card (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                user_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
                category VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
                memory_key VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
                version INT NOT NULL,
                value_json TEXT NOT NULL,
                confidence DOUBLE NOT NULL,
                source_session_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
                source_event_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
                valid_from TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                valid_to TIMESTAMP(6) NULL,
                sensitivity VARCHAR(32) NOT NULL DEFAULT 'normal',
                updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ON UPDATE CURRENT_TIMESTAMP(6),
                source_refs_json TEXT NULL,
                UNIQUE KEY user_memory_version (user_id, category, memory_key, version),
                KEY user_memory_active (user_id, valid_to, updated_at),
                KEY user_memory_source (user_id, source_session_id, source_event_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
            )
            cursor.execute(
                """CREATE TABLE IF NOT EXISTS user_memory_profile_lock (
                user_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
                category VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
                memory_key VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
                PRIMARY KEY (user_id, category, memory_key)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
            )
            cursor.execute(
                """CREATE TABLE IF NOT EXISTS user_memory_candidate (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                user_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
                source_session_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
                source_event_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
                candidate_key VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
                kind VARCHAR(16) NOT NULL,
                category VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
                memory_key VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
                value_json TEXT NOT NULL,
                confidence DOUBLE NOT NULL,
                sensitivity VARCHAR(32) NOT NULL DEFAULT 'normal',
                eligible TINYINT(1) NOT NULL DEFAULT 1,
                decision VARCHAR(16) NOT NULL DEFAULT 'PENDING',
                reason VARCHAR(256) NOT NULL DEFAULT '',
                created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ON UPDATE CURRENT_TIMESTAMP(6),
                UNIQUE KEY user_memory_candidate_once (user_id, source_event_id, candidate_key),
                KEY user_memory_candidate_retry (user_id, decision, updated_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
            )
            cursor.execute(
                """CREATE TABLE IF NOT EXISTS user_memory_episode (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                user_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
                source_session_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
                source_event_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
                summary_json TEXT NOT NULL,
                confidence DOUBLE NOT NULL,
                sensitivity VARCHAR(32) NOT NULL DEFAULT 'normal',
                archived_at TIMESTAMP(6) NULL,
                created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                UNIQUE KEY user_memory_episode_once (user_id, source_event_id),
                KEY user_memory_episode_recent (user_id, archived_at, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
            )
            # Safe additive upgrades for a table set up by the previous partial implementation.
            self._ensure_column(cursor, "user_memory_profile_card", "source_seq", "BIGINT NULL")
            self._ensure_column(cursor, "user_memory_profile_card", "source_created_at", "DATETIME(6) NULL")
            self._ensure_column(cursor, "user_memory_profile_card", "importance", "DOUBLE NOT NULL DEFAULT 0.5")
            self._ensure_column(cursor, "user_memory_profile_card", "source_refs_json", "TEXT NULL")
            self._ensure_column(cursor, "user_memory_candidate", "eligible", "TINYINT(1) NOT NULL DEFAULT 1")
            self._ensure_column(cursor, "user_memory_candidate", "source_seq", "BIGINT NULL")
            self._ensure_column(cursor, "user_memory_candidate", "source_created_at", "DATETIME(6) NULL")
            self._ensure_column(cursor, "user_memory_candidate", "attempts", "INT NOT NULL DEFAULT 0")
            self._ensure_column(cursor, "user_memory_candidate", "claim_token", "VARCHAR(64) NULL")
            self._ensure_column(cursor, "user_memory_candidate", "claim_until", "DATETIME(6) NULL")
            self._ensure_column(cursor, "user_memory_candidate", "last_error", "VARCHAR(64) NULL")
            self._ensure_column(cursor, "user_memory_episode", "source_seq", "BIGINT NULL")
            self._ensure_column(cursor, "user_memory_episode", "source_created_at", "DATETIME(6) NULL")
            self._ensure_column(cursor, "user_memory_episode", "importance", "DOUBLE NOT NULL DEFAULT 0.5")
            self._ensure_column(cursor, "user_memory_episode", "episode_type", "VARCHAR(16) NOT NULL DEFAULT 'source'")
            self._ensure_column(cursor, "user_memory_episode", "source_refs_json", "TEXT NULL")

        await self.db._call(create)

    async def source_event(self, user_id: str, session_id: str, event_id: str) -> dict[str, Any] | None:
        """Verify exact immutable USER_MESSAGE provenance and the current clear cutoff.

        No schema probing/fallback: an absent digest, mismatched owner/session,
        non-user event, malformed payload, or cleared event fails closed.
        """
        if not event_id.isdecimal() or int(event_id) <= 0:
            return None

        def read(_connection, cursor):
            cursor.execute(
                """SELECT e.event_id, e.session_id, e.user_id, e.seq, e.event_type,
                          e.payload, e.created_at, d.cutoff_seq, d.user_id AS digest_user_id
                   FROM conversation_event AS e
                   JOIN session_digest AS d ON d.session_id=e.session_id
                   WHERE e.event_id=%s AND e.session_id=%s AND e.user_id=%s
                     AND d.user_id=%s LIMIT 1""",
                (int(event_id), session_id, user_id, user_id),
            )
            row = cursor.fetchone()
            if not row or row["event_type"] != "USER_MESSAGE" or int(row["seq"]) <= int(row["cutoff_seq"] or 0):
                return None
            payload = _json_loads_any(row["payload"])
            if not isinstance(payload, dict) or payload.get("synthetic") or payload.get("role", "user") != "user":
                return None
            content = payload.get("content")
            if not isinstance(content, str):
                return None
            return {
                "user_id": str(row["user_id"]), "session_id": row["session_id"],
                "event_id": str(row["event_id"]), "seq": int(row["seq"]),
                "event_type": row["event_type"], "content": content,
                "created_at": row["created_at"],
            }

        return await self.db._call(read)

    async def verify_source(self, user_id: str, session_id: str, event_id: str) -> bool:
        return await self.source_event(user_id, session_id, event_id) is not None

    async def profile_cards(self, user_id: str, limit: int = 3) -> list[dict[str, Any]]:
        limit = _bounded_limit(limit, default=3, maximum=50)

        def read(_connection, cursor):
            cursor.execute(
                """SELECT * FROM user_memory_profile_card
                   WHERE user_id=%s AND valid_to IS NULL
                   ORDER BY importance DESC, confidence DESC, source_created_at DESC, id DESC
                   LIMIT %s""",
                (user_id, limit),
            )
            return [_card_from_row(row) for row in cursor.fetchall()]

        return await self.db._call(read)

    async def active_card(self, user_id: str, category: str, key: str) -> dict[str, Any] | None:
        def read(_connection, cursor):
            cursor.execute(
                """SELECT * FROM user_memory_profile_card
                   WHERE user_id=%s AND category=%s AND memory_key=%s AND valid_to IS NULL
                   ORDER BY version DESC LIMIT 1""",
                (user_id, category, key),
            )
            row = cursor.fetchone()
            return _card_from_row(row) if row else None

        return await self.db._call(read)

    async def record_candidate(self, candidate: MemoryCandidate) -> tuple[str, bool]:
        def insert(_connection, cursor):
            cursor.execute(
                """INSERT INTO user_memory_candidate
                   (user_id, source_session_id, source_event_id, candidate_key, kind, category,
                    memory_key, value_json, confidence, sensitivity, eligible, reason, source_seq, source_created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)""",
                (
                    candidate.user_id, candidate.source_session_id, candidate.source_event_id,
                    candidate.candidate_key, candidate.kind.value, candidate.category, candidate.key,
                    _json_dumps(candidate.value_json), candidate.confidence, candidate.sensitivity,
                    int(candidate.eligible), candidate.reason[:256], candidate.source_seq,
                    _as_db_datetime(candidate.source_created_at),
                ),
            )
            return str(cursor.lastrowid), cursor.rowcount == 1

        return await self.db._call(insert)

    async def claim_pending(self, user_id: str, limit: int, lease_seconds: int = 60) -> list[PendingCandidate]:
        bounded = _bounded_limit(limit, default=20, maximum=100)
        token = uuid.uuid4().hex

        def claim(_connection, cursor):
            cursor.execute(
                """SELECT * FROM user_memory_candidate
                   WHERE user_id=%s AND (decision='PENDING' OR
                     (decision='CLAIMED' AND claim_until < CURRENT_TIMESTAMP(6)))
                   ORDER BY id LIMIT %s FOR UPDATE""",
                (user_id, bounded),
            )
            rows = list(cursor.fetchall())
            claimed: list[PendingCandidate] = []
            for row in rows:
                cursor.execute(
                    """UPDATE user_memory_candidate
                       SET decision='CLAIMED', claim_token=%s,
                           claim_until=DATE_ADD(CURRENT_TIMESTAMP(6), INTERVAL %s SECOND)
                       WHERE id=%s""",
                    (token, max(1, min(int(lease_seconds), 3600)), row["id"]),
                )
                claimed.append(PendingCandidate(
                    str(row["id"]), _candidate_from_row(row), token, int(row.get("attempts") or 0),
                ))
            return claimed

        return await self.db._call(claim)

    async def pending_user_ids(self, limit: int = 50) -> list[str]:
        """Sweepable claim source: owners with PENDING or claim-expired rows."""
        bounded = _bounded_limit(limit, default=50, maximum=500)

        def sweep(_connection, cursor):
            cursor.execute(
                """SELECT user_id FROM user_memory_candidate
                   WHERE decision='PENDING' OR
                     (decision='CLAIMED' AND claim_until < CURRENT_TIMESTAMP(6))
                   GROUP BY user_id ORDER BY MIN(id) LIMIT %s""",
                (bounded,),
            )
            return [str(row["user_id"]) for row in cursor.fetchall()]

        return await self.db._call(sweep)

    async def mark_candidate(
        self, candidate_id: str, decision: MemoryDecision, reason: str, claim_token: str
    ) -> None:
        def update(_connection, cursor):
            cursor.execute(
                """UPDATE user_memory_candidate
                   SET decision=%s, reason=%s, claim_token=NULL, claim_until=NULL, last_error=NULL
                   WHERE id=%s AND decision='CLAIMED' AND claim_token=%s""",
                (decision.value, reason[:256], candidate_id, claim_token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("memory candidate claim was lost")

        await self.db._call(update)

    async def release_candidate(self, candidate_id: str, claim_token: str, error_type: str) -> None:
        def release(_connection, cursor):
            cursor.execute(
                """UPDATE user_memory_candidate
                   SET decision='PENDING', attempts=attempts+1, claim_token=NULL,
                       claim_until=NULL, last_error=%s
                   WHERE id=%s AND decision='CLAIMED' AND claim_token=%s""",
                (error_type[:64], candidate_id, claim_token),
            )

        await self.db._call(release)

    async def apply_profile_candidate(self, candidate: MemoryCandidate) -> tuple[MemoryDecision, str]:
        policy = MemoryPolicy()

        def apply(_connection, cursor):
            # This durable registry row serializes the absent-first-card case too.
            cursor.execute(
                """INSERT INTO user_memory_profile_lock (user_id,category,memory_key)
                   VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE memory_key=VALUES(memory_key)""",
                (candidate.user_id, candidate.category, candidate.key),
            )
            cursor.execute(
                """SELECT memory_key FROM user_memory_profile_lock
                   WHERE user_id=%s AND category=%s AND memory_key=%s FOR UPDATE""",
                (candidate.user_id, candidate.category, candidate.key),
            )
            cursor.execute(
                """SELECT * FROM user_memory_profile_card
                   WHERE user_id=%s AND category=%s AND memory_key=%s
                   ORDER BY version DESC FOR UPDATE""",
                (candidate.user_id, candidate.category, candidate.key),
            )
            rows = list(cursor.fetchall())
            active_row = next((row for row in rows if row.get("valid_to") is None), None)
            active = _card_from_row(active_row) if active_row else None
            inactive_row = rows[0] if rows and active_row is None else None
            inactive = _card_from_row(inactive_row) if inactive_row else None
            decision, reason = policy.decide_profile(candidate, active, inactive)
            if decision == MemoryDecision.IGNORE:
                return decision, reason

            source_time = _as_db_datetime(candidate.source_created_at)
            importance = _profile_importance(candidate)
            if decision == MemoryDecision.MERGE and active_row:
                new_source = _source_order(candidate.source_created_at, candidate.source_event_id) > _source_order(
                    active.get("source_created_at") or active.get("valid_from"), active.get("source_event_id")
                )
                cursor.execute(
                    """UPDATE user_memory_profile_card SET confidence=GREATEST(confidence,%s),
                           importance=GREATEST(importance,%s),
                           source_session_id=IF(%s, %s, source_session_id),
                           source_event_id=IF(%s, %s, source_event_id),
                           source_seq=IF(%s, %s, source_seq),
                           source_created_at=IF(%s, %s, source_created_at),
                           source_refs_json=%s, updated_at=CURRENT_TIMESTAMP(6)
                       WHERE id=%s""",
                    (candidate.confidence, importance,
                     new_source, candidate.source_session_id, new_source, candidate.source_event_id,
                     new_source, candidate.source_seq, new_source, source_time,
                     _json_dumps(_unique_refs(
                         (_json_loads_list(active_row.get("source_refs_json")) or [_source_ref_from_row(active_row)])
                         + [_source_ref(candidate)]
                     )), active_row["id"]),
                )
                return decision, reason

            if active_row:
                cursor.execute(
                    "UPDATE user_memory_profile_card SET valid_to=%s, updated_at=CURRENT_TIMESTAMP(6) WHERE id=%s",
                    (source_time, active_row["id"]),
                )
            next_version = max((int(row["version"]) for row in rows), default=0) + 1
            cursor.execute(
                """INSERT INTO user_memory_profile_card
                   (user_id,category,memory_key,version,value_json,confidence,source_session_id,
                    source_event_id,source_seq,source_created_at,valid_from,valid_to,sensitivity,importance,source_refs_json)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s)""",
                (candidate.user_id, candidate.category, candidate.key, next_version,
                 _json_dumps(candidate.value_json), candidate.confidence, candidate.source_session_id,
                 candidate.source_event_id, candidate.source_seq, source_time, source_time,
                 candidate.sensitivity, importance, _json_dumps([_source_ref(candidate)])),
            )
            return decision, reason

        return await self.db._call(apply)

    async def add_episode(self, candidate: MemoryCandidate) -> bool:
        source_time = _as_db_datetime(candidate.source_created_at)
        refs = [_source_ref(candidate)]

        def insert(_connection, cursor):
            cursor.execute(
                """INSERT INTO user_memory_episode
                   (user_id,source_session_id,source_event_id,summary_json,confidence,sensitivity,
                    source_seq,source_created_at,importance,episode_type,source_refs_json)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'source',%s)
                   ON DUPLICATE KEY UPDATE id=id""",
                (candidate.user_id, candidate.source_session_id, candidate.source_event_id,
                 _json_dumps(candidate.value_json), candidate.confidence, candidate.sensitivity,
                 candidate.source_seq, source_time, _episode_importance(candidate), _json_dumps(refs)),
            )
            return cursor.rowcount == 1

        return bool(await self.db._call(insert))

    async def episodes(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        bounded = _bounded_limit(limit, default=20, maximum=100)

        def read(_connection, cursor):
            cursor.execute(
                """SELECT ep.*, ev.seq AS live_source_seq, ev.created_at AS live_source_created_at
                   FROM user_memory_episode AS ep
                   JOIN conversation_event AS ev
                     ON ev.event_id=CAST(ep.source_event_id AS UNSIGNED)
                    AND ev.session_id=ep.source_session_id AND ev.user_id=ep.user_id
                   JOIN session_digest AS d ON d.session_id=ev.session_id AND d.user_id=ev.user_id
                   WHERE ep.user_id=%s AND ep.archived_at IS NULL
                     AND ev.event_type='USER_MESSAGE' AND ev.seq>d.cutoff_seq
                   ORDER BY ep.importance DESC, ev.created_at DESC, ep.id DESC LIMIT %s""",
                (user_id, min(400, bounded * 4)),
            )
            rows = list(cursor.fetchall())
            output = []
            for row in rows:
                episode = _episode_from_row(row)
                refs = episode.get("source_refs") or [_source_ref_from_episode(episode)]
                if refs and all(self._source_ref_is_live(cursor, user_id, ref) for ref in refs):
                    self._remove_stale_derived_abstraction(cursor, user_id, episode)
                    output.append(episode)
                    if len(output) >= bounded:
                        break
            return output

        return await self.db._call(read)

    async def search_episodes(
        self, user_id: str, query_terms: list[str], limit: int = 500
    ) -> list[dict[str, Any]]:
        """Fetch only owner-owned episodes matching at least one query keyword.

        Filtering happens in MySQL before the bounded candidate window, so a
        stream of newer unrelated episodes cannot hide an older relevant source.
        LOCATE receives parameters (not interpolated query text), avoiding LIKE
        wildcard semantics while retaining a lightweight indexed-owner scan.
        """
        terms = _search_terms(query_terms)
        if not terms:
            return []
        bounded = _bounded_limit(limit, default=500, maximum=1000)
        predicate = " OR ".join("LOCATE(%s, LOWER(ep.summary_json)) > 0" for _ in terms)
        score_expression = " + ".join(
            "CASE WHEN LOCATE(%s, LOWER(ep.summary_json)) > 0 THEN 1 ELSE 0 END" for _ in terms
        )

        def read(_connection, cursor):
            cursor.execute(
                f"""SELECT ep.*, ev.seq AS live_source_seq, ev.created_at AS live_source_created_at,
                           ({score_expression}) AS lexical_matches
                    FROM user_memory_episode AS ep
                    JOIN conversation_event AS ev
                      ON ev.event_id=CAST(ep.source_event_id AS UNSIGNED)
                     AND ev.session_id=ep.source_session_id AND ev.user_id=ep.user_id
                    JOIN session_digest AS d ON d.session_id=ev.session_id AND d.user_id=ev.user_id
                    WHERE ep.user_id=%s AND ep.archived_at IS NULL
                      AND ev.event_type='USER_MESSAGE' AND ev.seq>d.cutoff_seq
                      AND ({predicate})
                    ORDER BY lexical_matches DESC, ep.importance DESC,
                             ev.created_at DESC, ep.id DESC LIMIT %s""",
                (*terms, user_id, *terms, bounded),
            )
            rows = list(cursor.fetchall())
            output = []
            for row in rows:
                episode = _episode_from_row(row)
                refs = episode.get("source_refs") or [_source_ref_from_episode(episode)]
                if refs and all(self._source_ref_is_live(cursor, user_id, ref) for ref in refs):
                    self._remove_stale_derived_abstraction(cursor, user_id, episode)
                    episode["lexical_matches"] = int(row.get("lexical_matches") or 0)
                    output.append(episode)
            return output

        return await self.db._call(read)

    @classmethod
    def _remove_stale_derived_abstraction(cls, cursor, user_id: str, episode: dict[str, Any]) -> None:
        value = episode.get("value_json")
        derived = value.get("derived_abstraction") if isinstance(value, dict) else None
        refs = derived.get("source_refs") if isinstance(derived, dict) else None
        if refs and not all(cls._source_ref_is_live(cursor, user_id, ref) for ref in refs):
            value.pop("derived_abstraction", None)

    @staticmethod
    def _source_ref_is_live(cursor, user_id: str, ref: dict[str, Any]) -> bool:
        event_id = str(ref.get("event_id", ""))
        session_id = ref.get("session_id")
        if not event_id.isdecimal() or not session_id:
            return False
        cursor.execute(
            """SELECT e.event_id FROM conversation_event AS e
               JOIN session_digest AS d ON d.session_id=e.session_id AND d.user_id=e.user_id
               WHERE e.event_id=%s AND e.session_id=%s AND e.user_id=%s
                 AND e.event_type='USER_MESSAGE' AND e.seq>d.cutoff_seq LIMIT 1""",
            (int(event_id), session_id, user_id),
        )
        return cursor.fetchone() is not None

    async def consolidate(self, user_id: str) -> dict[str, Any]:
        def run(_connection, cursor):
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            cursor.execute(
                """SELECT * FROM user_memory_profile_card
                   WHERE user_id=%s AND valid_to IS NULL
                   ORDER BY category,memory_key,source_created_at DESC,importance DESC,id DESC FOR UPDATE""",
                (user_id,),
            )
            profile_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for row in cursor.fetchall():
                profile_groups.setdefault((row["category"], row["memory_key"]), []).append(row)
            conflicts = 0
            for rows in profile_groups.values():
                for stale in rows[1:]:
                    cursor.execute(
                        "UPDATE user_memory_profile_card SET valid_to=%s WHERE id=%s AND valid_to IS NULL",
                        (rows[0].get("source_created_at") or now, stale["id"]),
                    )
                    conflicts += cursor.rowcount

            cursor.execute(
                """SELECT * FROM user_memory_episode
                   WHERE user_id=%s AND archived_at IS NULL ORDER BY source_created_at DESC,id DESC FOR UPDATE""",
                (user_id,),
            )
            rows = list(cursor.fetchall())
            active: list[dict[str, Any]] = []
            archived = 0
            expired = 0
            for raw in rows:
                episode = _episode_from_row(raw)
                refs = episode.get("source_refs") or [_source_ref_from_episode(episode)]
                if not refs or not all(self._source_ref_is_live(cursor, user_id, ref) for ref in refs):
                    cursor.execute("UPDATE user_memory_episode SET archived_at=%s WHERE id=%s", (now, raw["id"]))
                    archived += 1
                    continue
                source_time = _parse_dt(raw.get("source_created_at") or raw.get("created_at"))
                importance = float(raw.get("importance") or 0.0)
                if source_time and now - source_time.replace(tzinfo=None) > timedelta(days=365) and importance < 0.35:
                    cursor.execute("UPDATE user_memory_episode SET archived_at=%s WHERE id=%s", (now, raw["id"]))
                    archived += 1
                    expired += 1
                    continue
                active.append(raw)

            groups: dict[str, list[dict[str, Any]]] = {}
            for row in active:
                groups.setdefault(
                    _episode_dedup_key(_json_loads_any(row["summary_json"]), row.get("source_event_id")), []
                ).append(row)
            deduplicated = 0
            merged = 0
            deduped_active: list[dict[str, Any]] = []
            for group in groups.values():
                group.sort(key=lambda row: (
                    float(row.get("importance") or 0.0),
                    _source_order(row.get("source_created_at") or row.get("created_at"), row.get("source_event_id")),
                ), reverse=True)
                winner = group[0]
                refs = _unique_refs(
                    ref for item in group
                    for ref in (_json_loads_list(item.get("source_refs_json")) or [_source_ref_from_row(item)])
                )
                if len(group) > 1:
                    cursor.execute(
                        """UPDATE user_memory_episode SET source_refs_json=%s,
                               importance=GREATEST(importance,%s),confidence=GREATEST(confidence,%s),
                               episode_type='merged' WHERE id=%s""",
                        (_json_dumps(refs), max(float(item.get("importance") or 0.0) for item in group),
                         max(float(item.get("confidence") or 0.0) for item in group), winner["id"]),
                    )
                    for duplicate in group[1:]:
                        cursor.execute("UPDATE user_memory_episode SET archived_at=%s WHERE id=%s", (now, duplicate["id"]))
                        deduplicated += 1
                        archived += 1
                    merged += len(group) - 1
                winner["source_refs_json"] = _json_dumps(refs)
                winner["_source_refs"] = refs
                deduped_active.append(winner)

            live_refs = _unique_refs(ref for row in deduped_active for ref in row.get("_source_refs", []))
            abstracted = 0
            if len(live_refs) >= 3 and deduped_active:
                latest = max(deduped_active, key=lambda row: _source_order(
                    row.get("source_created_at") or row.get("created_at"), row.get("source_event_id")))
                topics = sorted({topic for row in deduped_active
                                 for topic in _json_loads_any(row["summary_json"]).get("topics", [])})
                labels = {"order": "order/订单", "refund": "refund/退款", "payment": "payment/支付",
                          "account_status": "account status/账户状态"}
                original = _json_loads_any(latest["summary_json"])
                derived = {
                    "summary": f"Related historical mentions covered {len(live_refs)} source statements and topics: "
                               + ", ".join(labels.get(topic, topic) for topic in topics)
                               + "; this is not current business state.",
                    "topics": topics,
                    "source_refs": live_refs,
                    "historical": True,
                    "not_authoritative": True,
                    "requires_live_tool_lookup": True,
                }
                original["derived_abstraction"] = derived
                cursor.execute(
                    """UPDATE user_memory_episode SET summary_json=%s,
                           importance=GREATEST(importance,%s) WHERE id=%s""",
                    (_json_dumps(original),
                     max(float(row.get("importance") or 0.0) for row in deduped_active), latest["id"]),
                )
                # The abstraction is supplemental metadata: every distinct source
                # episode remains independently searchable and keeps its own provenance.
                abstracted = 1
            elif len(deduped_active) > 100:
                ranked = sorted(deduped_active, key=lambda row: (
                    float(row.get("importance") or 0.0),
                    _source_order(row.get("source_created_at") or row.get("created_at"), row.get("source_event_id")),
                ), reverse=True)
                for row in ranked[100:]:
                    cursor.execute("UPDATE user_memory_episode SET archived_at=%s WHERE id=%s", (now, row["id"]))
                    archived += 1

            return {
                "importance_scored": len(profile_groups) + len(active), "deduplicated": deduplicated,
                "conflicts": conflicts, "merged": merged, "abstracted": abstracted,
                "expired": expired, "archived": archived,
            }

        return await self.db._call(run)

    async def deactivate_profile(self, user_id: str, category: str, key: str) -> bool:
        def deactivate(_connection, cursor):
            cursor.execute(
                """INSERT INTO user_memory_profile_lock(user_id,category,memory_key)
                   VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE memory_key=VALUES(memory_key)""",
                (user_id, category, key),
            )
            cursor.execute(
                """SELECT memory_key FROM user_memory_profile_lock
                   WHERE user_id=%s AND category=%s AND memory_key=%s FOR UPDATE""",
                (user_id, category, key),
            )
            cursor.execute(
                """UPDATE user_memory_profile_card SET valid_to=CURRENT_TIMESTAMP(6),
                       updated_at=CURRENT_TIMESTAMP(6)
                   WHERE user_id=%s AND category=%s AND memory_key=%s AND valid_to IS NULL""",
                (user_id, category, key),
            )
            return cursor.rowcount > 0

        return bool(await self.db._call(deactivate))

    @staticmethod
    def _ensure_column(cursor, table: str, column: str, definition: str) -> None:
        cursor.execute(
            """SELECT 1 FROM information_schema.columns
               WHERE table_schema=DATABASE() AND table_name=%s AND column_name=%s""",
            (table, column),
        )
        if cursor.fetchone() is None:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


@dataclass
class InMemoryUserMemoryRepository:
    """Explicit deterministic test repository; never selected implicitly."""

    sources: set[tuple[str, str, str]] = field(default_factory=set)
    source_rows: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    cutoffs: dict[tuple[str, str], int] = field(default_factory=dict)
    cards: list[dict[str, Any]] = field(default_factory=list)
    candidates: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    episode_rows: list[dict[str, Any]] = field(default_factory=list)
    _locks: dict[tuple[str, str, str], asyncio.Lock] = field(default_factory=dict)
    _queue_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def add_source(
        self, user_id: str | int, session_id: str, event_id: str | int,
        content: str | None = None, *, seq: int | None = None,
        created_at: datetime | str | None = None, event_type: str = "USER_MESSAGE",
        synthetic: bool = False,
    ) -> None:
        owner, event_id = _normalize_user_id(user_id), str(event_id)
        marker = (owner, session_id, event_id)
        self.sources.add(marker)
        if seq is None:
            seq = 1 + max((row["seq"] for key, row in self.source_rows.items()
                           if key[:2] == (owner, session_id)), default=0)
        self.source_rows[marker] = {
            "user_id": owner, "session_id": session_id, "event_id": event_id,
            "seq": int(seq), "event_type": event_type, "content": content,
            "synthetic": synthetic,
            "created_at": _parse_dt(created_at) or datetime.now(timezone.utc),
        }

    def clear_through(self, user_id: str | int, session_id: str, seq: int) -> None:
        self.cutoffs[(_normalize_user_id(user_id), session_id)] = max(
            int(seq), self.cutoffs.get((_normalize_user_id(user_id), session_id), 0)
        )

    async def initialize(self) -> None:
        return None

    async def source_event(self, user_id: str, session_id: str, event_id: str) -> dict[str, Any] | None:
        row = self.source_rows.get((user_id, session_id, event_id))
        if not row or row["event_type"] != "USER_MESSAGE" or row.get("synthetic") or not isinstance(row.get("content"), str):
            return None
        if row["seq"] <= self.cutoffs.get((user_id, session_id), 0):
            return None
        return {**row}

    async def verify_source(self, user_id: str, session_id: str, event_id: str) -> bool:
        return await self.source_event(user_id, session_id, event_id) is not None

    async def profile_cards(self, user_id: str, limit: int = 3) -> list[dict[str, Any]]:
        active = [card for card in self.cards if card["user_id"] == user_id and card.get("valid_to") is None]
        active.sort(key=lambda card: (card["importance"], card["confidence"],
                                      _source_order(card.get("source_created_at"), card.get("source_event_id"))), reverse=True)
        return [_copy_jsonable(card) for card in active[:_bounded_limit(limit, default=3, maximum=50)]]

    async def active_card(self, user_id: str, category: str, key: str) -> dict[str, Any] | None:
        cards = [card for card in self.cards if card["user_id"] == user_id and card["category"] == category
                 and card["key"] == key and card.get("valid_to") is None]
        cards.sort(key=lambda card: card["version"], reverse=True)
        return _copy_jsonable(cards[0]) if cards else None

    async def record_candidate(self, candidate: MemoryCandidate) -> tuple[str, bool]:
        marker = (candidate.user_id, candidate.source_event_id, candidate.candidate_key)
        if marker in self.candidates:
            return self.candidates[marker]["id"], False
        candidate_id = str(len(self.candidates) + 1)
        self.candidates[marker] = {
            "id": candidate_id, "candidate": candidate, "user_id": candidate.user_id,
            "source_session_id": candidate.source_session_id, "source_event_id": candidate.source_event_id,
            "candidate_key": candidate.candidate_key, "decision": "PENDING", "reason": candidate.reason,
            "attempts": 0, "claim_token": None,
        }
        return candidate_id, True

    async def claim_pending(self, user_id: str, limit: int, lease_seconds: int = 60) -> list[PendingCandidate]:
        token, now = uuid.uuid4().hex, datetime.now(timezone.utc)
        async with self._queue_lock:
            eligible = []
            for row in self.candidates.values():
                expired_claim = row["decision"] == "CLAIMED" and row.get("claim_until") and row["claim_until"] <= now
                if row["user_id"] == user_id and (row["decision"] == "PENDING" or expired_claim):
                    eligible.append(row)
            eligible.sort(key=lambda row: int(row["id"]))
            claimed = []
            for row in eligible[:_bounded_limit(limit, default=20, maximum=100)]:
                row["decision"], row["claim_token"] = "CLAIMED", token
                row["claim_until"] = now + timedelta(seconds=max(1, lease_seconds))
                claimed.append(PendingCandidate(row["id"], row["candidate"], token, row["attempts"]))
            return claimed

    async def pending_user_ids(self, limit: int = 50) -> list[str]:
        now = datetime.now(timezone.utc)
        async with self._queue_lock:
            first_pending: dict[str, int] = {}
            for row in self.candidates.values():
                expired_claim = row["decision"] == "CLAIMED" and row.get("claim_until") and row["claim_until"] <= now
                if row["decision"] == "PENDING" or expired_claim:
                    first_pending.setdefault(row["user_id"], int(row["id"]))
            ordered = sorted(first_pending, key=lambda user: first_pending[user])
            return ordered[:_bounded_limit(limit, default=50, maximum=500)]

    async def mark_candidate(
        self, candidate_id: str, decision: MemoryDecision, reason: str, claim_token: str
    ) -> None:
        for row in self.candidates.values():
            if row["id"] == candidate_id and row.get("claim_token") == claim_token and row["decision"] == "CLAIMED":
                row["decision"], row["reason"], row["claim_token"] = decision.value, reason, None
                row.pop("claim_until", None)
                return
        raise RuntimeError("memory candidate claim was lost")

    async def release_candidate(self, candidate_id: str, claim_token: str, error_type: str) -> None:
        for row in self.candidates.values():
            if row["id"] == candidate_id and row.get("claim_token") == claim_token and row["decision"] == "CLAIMED":
                row["decision"], row["claim_token"] = "PENDING", None
                row["attempts"] += 1
                row["last_error"] = error_type[:64]
                row.pop("claim_until", None)
                return

    async def apply_profile_candidate(self, candidate: MemoryCandidate) -> tuple[MemoryDecision, str]:
        key = (candidate.user_id, candidate.category, candidate.key)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            all_cards = [card for card in self.cards if card["user_id"] == candidate.user_id
                         and card["category"] == candidate.category and card["key"] == candidate.key]
            active = await self.active_card(candidate.user_id, candidate.category, candidate.key)
            latest = max(all_cards, key=lambda card: card["version"], default=None)
            decision, reason = MemoryPolicy().decide_profile(candidate, active, latest if active is None else None)
            if decision == MemoryDecision.IGNORE:
                return decision, reason
            now = _now_dt()
            importance = _profile_importance(candidate)
            if decision == MemoryDecision.MERGE and active:
                for card in self.cards:
                    if card["id"] == active["id"]:
                        card["confidence"] = max(card["confidence"], candidate.confidence)
                        card["importance"] = max(card["importance"], importance)
                        if _source_order(candidate.source_created_at, candidate.source_event_id) > _source_order(
                            active.get("source_created_at") or active.get("valid_from"), active.get("source_event_id")
                        ):
                            card["source_session_id"] = candidate.source_session_id
                            card["source_event_id"] = candidate.source_event_id
                            card["source_seq"] = candidate.source_seq
                            card["source_created_at"] = _source_iso(candidate.source_created_at)
                        card["source_refs"] = _unique_refs(card.get("source_refs", [_source_ref_from_episode(card)]) + [_source_ref(candidate)])
                        card["updated_at"] = now.isoformat()
                        break
                return decision, reason
            if active:
                for card in self.cards:
                    if card["id"] == active["id"]:
                        card["valid_to"] = _source_iso(candidate.source_created_at)
                        card["updated_at"] = now.isoformat()
                        break
            version = max((card["version"] for card in all_cards), default=0) + 1
            self.cards.append({
                "id": str(len(self.cards) + 1), "user_id": candidate.user_id,
                "category": candidate.category, "key": candidate.key, "version": version,
                "value_json": _copy_jsonable(candidate.value_json), "confidence": candidate.confidence,
                "source_session_id": candidate.source_session_id, "source_event_id": candidate.source_event_id,
                "source_seq": candidate.source_seq, "source_created_at": _source_iso(candidate.source_created_at),
                "valid_from": _source_iso(candidate.source_created_at), "valid_to": None,
                "sensitivity": candidate.sensitivity, "importance": importance,
                "source_refs": [_source_ref(candidate)], "updated_at": now.isoformat(),
            })
            return decision, reason

    async def add_episode(self, candidate: MemoryCandidate) -> bool:
        if any(row["user_id"] == candidate.user_id and row["source_event_id"] == candidate.source_event_id
               for row in self.episode_rows):
            return False
        self.episode_rows.append({
            "id": str(len(self.episode_rows) + 1), "user_id": candidate.user_id,
            "source_session_id": candidate.source_session_id, "source_event_id": candidate.source_event_id,
            "source_seq": candidate.source_seq, "source_created_at": _source_iso(candidate.source_created_at),
            "value_json": _copy_jsonable(candidate.value_json), "confidence": candidate.confidence,
            "importance": _episode_importance(candidate), "sensitivity": candidate.sensitivity,
            "source_refs": [_source_ref(candidate)], "episode_type": "source", "archived_at": None,
            "created_at": _now_dt().isoformat(), "historical": True, "authoritative": False,
        })
        return True

    async def episodes(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = []
        for row in self.episode_rows:
            if row["user_id"] != user_id or row.get("archived_at") is not None:
                continue
            refs = row.get("source_refs") or [_source_ref_from_episode(row)]
            live = bool(refs)
            for ref in refs:
                if not await self._source_ref_is_live(user_id, ref):
                    live = False
                    break
            if live:
                item = _copy_jsonable(row)
                item["value_json"] = item.get("value_json", {})
                derived = item["value_json"].get("derived_abstraction")
                derived_refs = derived.get("source_refs", []) if isinstance(derived, dict) else []
                if derived_refs and not all(
                    [await self._source_ref_is_live(user_id, ref) for ref in derived_refs]
                ):
                    item["value_json"].pop("derived_abstraction", None)
                rows.append(item)
        rows.sort(key=lambda row: (row.get("importance", 0), _source_order(row.get("source_created_at"), row.get("source_event_id"))), reverse=True)
        return rows[:_bounded_limit(limit, default=20, maximum=100)]

    async def search_episodes(
        self, user_id: str, query_terms: list[str], limit: int = 500
    ) -> list[dict[str, Any]]:
        terms = set(_search_terms(query_terms))
        if not terms:
            return []
        matches = []
        for row in self.episode_rows:
            if row["user_id"] != user_id or row.get("archived_at") is not None:
                continue
            refs = row.get("source_refs") or [_source_ref_from_episode(row)]
            if not refs:
                continue
            live = True
            for ref in refs:
                if not await self._source_ref_is_live(user_id, ref):
                    live = False
                    break
            if not live:
                continue
            value = _copy_jsonable(row.get("value_json", {}))
            derived = value.get("derived_abstraction") if isinstance(value, dict) else None
            derived_refs = derived.get("source_refs", []) if isinstance(derived, dict) else []
            if derived_refs and not all(
                [await self._source_ref_is_live(user_id, ref) for ref in derived_refs]
            ):
                value.pop("derived_abstraction", None)
            lexical_score = _score(terms, _semantic_text(value))
            if lexical_score > 0:
                candidate = _copy_jsonable(row)
                candidate["value_json"] = value
                candidate["lexical_matches"] = len(terms & set(_tokens(_semantic_text(value))))
                matches.append((lexical_score, candidate))
        matches.sort(key=lambda item: (
            item[0], item[1].get("importance", 0),
            _source_order(item[1].get("source_created_at"), item[1].get("source_event_id")),
        ), reverse=True)
        bounded = _bounded_limit(limit, default=500, maximum=1000)
        return [row for _, row in matches[:bounded]]

    async def _source_ref_is_live(self, user_id: str, ref: dict[str, Any]) -> bool:
        row = self.source_rows.get((user_id, str(ref.get("session_id", "")), str(ref.get("event_id", ""))))
        return bool(row and row["event_type"] == "USER_MESSAGE"
                    and row["seq"] > self.cutoffs.get((user_id, row["session_id"]), 0))

    async def consolidate(self, user_id: str) -> dict[str, Any]:
        now = _now_dt()
        active_profiles: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for card in self.cards:
            if card["user_id"] == user_id and card.get("valid_to") is None:
                active_profiles.setdefault((card["category"], card["key"]), []).append(card)
        conflicts = 0
        for cards in active_profiles.values():
            cards.sort(key=lambda card: (_source_order(card.get("source_created_at"), card.get("source_event_id")), card["version"]), reverse=True)
            for stale in cards[1:]:
                stale["valid_to"] = cards[0].get("source_created_at") or now.isoformat()
                conflicts += 1

        eligible: list[dict[str, Any]] = []
        archived = expired = 0
        for row in self.episode_rows:
            if row["user_id"] != user_id or row.get("archived_at") is not None:
                continue
            refs = row.get("source_refs") or [_source_ref_from_episode(row)]
            live = bool(refs) and all([await self._source_ref_is_live(user_id, ref) for ref in refs])
            if not live:
                row["archived_at"] = now.isoformat()
                archived += 1
                continue
            source_time = _parse_dt(row.get("source_created_at") or row.get("created_at"))
            if source_time and now - source_time > timedelta(days=365) and row.get("importance", 0) < 0.35:
                row["archived_at"] = now.isoformat()
                expired += 1
                archived += 1
                continue
            eligible.append(row)

        groups: dict[str, list[dict[str, Any]]] = {}
        for row in eligible:
            groups.setdefault(
                _episode_dedup_key(row["value_json"], row.get("source_event_id")), []
            ).append(row)
        deduplicated = merged = 0
        deduped: list[dict[str, Any]] = []
        for rows in groups.values():
            rows.sort(key=lambda row: (row.get("importance", 0), _source_order(row.get("source_created_at"), row.get("source_event_id"))), reverse=True)
            winner = rows[0]
            winner["source_refs"] = _unique_refs(ref for row in rows for ref in row.get("source_refs", [_source_ref_from_episode(row)]))
            if len(rows) > 1:
                winner["confidence"] = max(row["confidence"] for row in rows)
                winner["importance"] = max(row["importance"] for row in rows)
                winner["episode_type"] = "merged"
                for duplicate in rows[1:]:
                    duplicate["archived_at"] = now.isoformat()
                    deduplicated += 1
                    archived += 1
                merged += len(rows) - 1
            deduped.append(winner)

        live_refs = _unique_refs(ref for row in deduped for ref in row.get("source_refs", []))
        abstracted = 0
        if len(live_refs) >= 3 and deduped:
            latest = max(deduped, key=lambda row: _source_order(row.get("source_created_at"), row.get("source_event_id")))
            topics = sorted({topic for row in deduped for topic in row.get("value_json", {}).get("topics", [])})
            labels = {"order": "order/订单", "refund": "refund/退款", "payment": "payment/支付",
                      "account_status": "account status/账户状态"}
            original = latest.get("value_json", {})
            original["derived_abstraction"] = {
                "summary": f"Related historical mentions covered {len(live_refs)} source statements and topics: "
                           + ", ".join(labels.get(topic, topic) for topic in topics)
                           + "; this is not current business state.",
                "topics": topics, "source_refs": live_refs, "historical": True,
                "not_authoritative": True, "requires_live_tool_lookup": True,
            }
            # Keep each distinct episode and its original claim searchable. This
            # abstraction is additive and never replaces or archives those sources.
            abstracted = 1
        elif len(deduped) > 100:
            ranked = sorted(deduped, key=lambda row: (row.get("importance", 0), _source_order(row.get("source_created_at"), row.get("source_event_id"))), reverse=True)
            for row in ranked[100:]:
                row["archived_at"] = now.isoformat()
                archived += 1

        return {
            "importance_scored": len(active_profiles) + len(eligible), "deduplicated": deduplicated,
            "conflicts": conflicts, "merged": merged, "abstracted": abstracted,
            "expired": expired, "archived": archived,
        }

    async def deactivate_profile(self, user_id: str, category: str, key: str) -> bool:
        lock = self._locks.setdefault((user_id, category, key), asyncio.Lock())
        async with lock:
            now = _now_dt().isoformat()
            found = False
            for card in self.cards:
                if card["user_id"] == user_id and card["category"] == category and card["key"] == key and card.get("valid_to") is None:
                    card["valid_to"] = now
                    card["updated_at"] = now
                    found = True
            return found


class UserMemoryService:
    """Source-verified owner-isolated memory with durable deferred ingestion."""

    def __init__(
        self,
        repository: UserMemoryRepository | None = None,
        *,
        database: PlatformDatabase | None = None,
        extractor: MemoryExtractor | None = None,
        policy: MemoryPolicy | None = None,
    ):
        if repository is None:
            # There is intentionally no implicit process-local production fallback.
            repository = MySQLUserMemoryRepository(database or PlatformDatabase.from_env())
        self.repository = repository
        self.extractor = extractor or MemoryExtractor()
        self.policy = policy or MemoryPolicy()

    async def initialize(self) -> None:
        await self.repository.initialize()

    async def profile_cards(self, user_id: str | int, limit: int = 3) -> list[dict[str, Any]]:
        return await self.repository.profile_cards(_normalize_user_id(user_id), limit=limit)

    async def retrieve(self, user_id: str | int, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        owner = _normalize_user_id(user_id)
        limit = _bounded_limit(top_k, default=3, maximum=20)
        query_terms = set(_tokens(query))
        if not query_terms:
            return []
        cards = await self.repository.profile_cards(owner, limit=50)
        # Retrieve a bounded candidate set after owner-scoped keyword filtering.
        # Do not fetch the newest N episodes and only then try lexical ranking.
        episodes = await self.repository.search_episodes(owner, sorted(query_terms), limit=500)
        scored: list[tuple[float, dict[str, Any]]] = []
        for card in cards:
            body = _semantic_text(card.get("value_json", {})) + " " + card.get("category", "") + " " + card.get("key", "")
            score = _score(query_terms, body)
            if score > 0:
                score += float(card.get("confidence", 0.0)) * 0.02
                result = _copy_jsonable(card)
                result.update(memory_type="profile", historical=False, authoritative=True,
                              not_authoritative=False, score=round(score, 6))
                scored.append((score, result))
        for episode in episodes:
            value = episode.get("value_json", episode.get("summary_json", {}))
            body = _semantic_text(value)
            score = _score(query_terms, body)
            if score > 0:
                score += float(episode.get("importance", episode.get("confidence", 0.0))) * 0.01
                result = _copy_jsonable(episode)
                result.update(memory_type="episode", historical=True, authoritative=False,
                              not_authoritative=True,
                              source_readonly_hint=value.get("source_readonly_hint") or
                                  "Historical source only; not authoritative for current business state.",
                              score=round(score, 6))
                scored.append((score, result))
        scored.sort(key=lambda item: (item[0], item[1].get("importance", 0), item[1].get("source_created_at", "")), reverse=True)
        return [item for _, item in scored[:limit]]

    async def process_message(
        self, user_id: str | int, session_id: str, event_id: str | int, content: str
    ) -> dict[str, Any]:
        """After-commit callback: verify a USER_MESSAGE and durably enqueue candidates.

        Calling this method does not apply memory. A bounded background worker
        should call ``process_pending(user_id, limit=...)`` after chat completion.
        It is safe to retry on callback failure because candidate inserts are
        idempotent. Never pass assistant/tool-generated text here.
        """
        owner, event_id = _normalize_user_id(user_id), str(event_id)
        if not isinstance(content, str):
            raise ValueError("content must be text")
        try:
            source = await self.repository.source_event(owner, session_id, event_id)
        except Exception as exc:
            return _enqueue_failure(owner, session_id, event_id, 0, exc)
        if source is None or source.get("event_id") != event_id or source.get("content") != content:
            raise MemoryProvenanceError("memory source must exactly match an owned, uncleared USER_MESSAGE event")

        extracted = self.extractor.extract(owner, session_id, event_id, content)
        if any(
            candidate.user_id != owner or candidate.source_session_id != session_id
            or candidate.source_event_id != event_id
            for candidate in extracted
        ):
            raise MemoryProvenanceError("extractor candidate owner/source differs from the verified event")
        candidates = [replace(
            candidate, source_seq=int(source["seq"]), source_created_at=source["created_at"]
        ) for candidate in extracted]
        queued = duplicates = 0
        for candidate in candidates:
            try:
                _, inserted = await self.repository.record_candidate(candidate)
                queued += int(inserted)
                duplicates += int(not inserted)
            except Exception as exc:
                return _enqueue_failure(owner, session_id, event_id, queued, exc, duplicates=duplicates)
        return {
            "status": "queued" if candidates else "no_candidates",
            "deferred": True,
            "user_id": owner,
            "session_id": session_id,
            "event_id": event_id,
            "candidate_count": len(candidates),
            "queued_count": queued,
            "existing_count": duplicates,
        }

    async def process_pending(self, user_id: str | int, limit: int = 20) -> dict[str, Any]:
        """Claim and process a bounded number of owner-scoped candidates.

        A failed candidate is released to PENDING when possible. If the worker
        dies, its lease expires and another worker can safely retry it. All
        failures are summarized without logging or returning memory contents.
        """
        owner = _normalize_user_id(user_id)
        pending = await self.repository.claim_pending(owner, _bounded_limit(limit, default=20, maximum=100))
        decisions: list[dict[str, str]] = []
        accepted = failed = 0
        for item in pending:
            candidate = item.candidate
            try:
                source = await self.repository.source_event(
                    owner, candidate.source_session_id, candidate.source_event_id
                )
                stored_time = _parse_dt(candidate.source_created_at)
                current_time = _parse_dt(source.get("created_at")) if source else None
                source_changed = bool(source and stored_time and current_time and stored_time != current_time)
                if (
                    source is None
                    or source.get("event_id") != candidate.source_event_id
                    or (candidate.source_seq is not None and int(source["seq"]) != candidate.source_seq)
                    or source_changed
                ):
                    decision, reason = MemoryDecision.IGNORE, "source_missing_mismatched_or_cleared"
                else:
                    candidate = replace(candidate, source_seq=int(source["seq"]), source_created_at=source["created_at"] )
                    if not candidate.eligible or candidate.sensitivity == "restricted":
                        decision, reason = MemoryDecision.IGNORE, candidate.reason
                    elif candidate.kind == CandidateKind.PROFILE:
                        decision, reason = await self.repository.apply_profile_candidate(candidate)
                    elif self.policy.episode_valid(candidate):
                        inserted = await self.repository.add_episode(candidate)
                        decision, reason = (MemoryDecision.ADD, candidate.reason) if inserted else (MemoryDecision.MERGE, "episode_already_persisted")
                    else:
                        decision, reason = MemoryDecision.IGNORE, "episode_policy_rejected"
                await self.repository.mark_candidate(item.candidate_id, decision, reason, item.claim_token)
                accepted += int(decision in {MemoryDecision.ADD, MemoryDecision.UPDATE, MemoryDecision.MERGE})
                decisions.append({"candidate_id": item.candidate_id, "decision": decision.value})
            except Exception as exc:
                failed += 1
                try:
                    await self.repository.release_candidate(item.candidate_id, item.claim_token, type(exc).__name__)
                except Exception:
                    # The durable claim lease is the recovery path if release storage is down.
                    pass
                decisions.append({"candidate_id": item.candidate_id, "decision": "RETRY", "error_type": type(exc).__name__})
        return {
            "status": "processed" if not failed else "partial_failure",
            "user_id": owner, "claimed_count": len(pending), "accepted_count": accepted,
            "failed_count": failed, "decisions": decisions,
        }

    async def drain_pending(self, user_id: str | int, limit: int = 20) -> dict[str, Any]:
        """Alias intended for a scheduled/after-response worker integration."""
        return await self.process_pending(user_id, limit=limit)

    async def pending_user_ids(self, limit: int = 50) -> list[str]:
        """Sweep owners with claimable candidates so a worker recovers after restart."""
        sweep = getattr(self.repository, "pending_user_ids", None)
        if not callable(sweep):
            return []
        users = await sweep(_bounded_limit(limit, default=50, maximum=500))
        return [_normalize_user_id(user) for user in users if isinstance(user, str) and user.strip()]

    async def consolidate(self, user_id: str | int) -> dict[str, Any]:
        return await self.repository.consolidate(_normalize_user_id(user_id))

    async def deactivate_profile(self, user_id: str | int, category: str, key: str) -> bool:
        """Explicitly deactivate a stable profile card without deleting its history."""
        return await self.repository.deactivate_profile(_normalize_user_id(user_id), category, key)


def _enqueue_failure(user_id: str, session_id: str, event_id: str, queued: int, exc: Exception, *, duplicates: int = 0) -> dict[str, Any]:
    return {
        "status": "enqueue_failed", "deferred": True, "retryable": True,
        "user_id": user_id, "session_id": session_id, "event_id": event_id,
        "queued_count": queued, "existing_count": duplicates,
        "failed_count": 1, "error_type": type(exc).__name__,
    }


def _normalize_user_id(user_id: str | int) -> str:
    if isinstance(user_id, bool) or user_id is None:
        raise ValueError("invalid user_id")
    value = str(user_id).strip()
    if not value or "\x00" in value or len(value) > 128:
        raise ValueError("invalid user_id")
    return value


def _bounded_limit(value: int, *, default: int, maximum: int) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(limit, maximum))


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_loads_any(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return {}
    return json.loads(value)


def _json_loads_list(value: Any) -> list[dict[str, Any]]:
    parsed = _json_loads_any(value) if value else []
    return parsed if isinstance(parsed, list) else []


def _canonical_json(value: Any) -> str:
    return _json_dumps(value)


def _copy_jsonable(value: Any) -> Any:
    return json.loads(_json_dumps(value))


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _as_db_datetime(value: Any) -> datetime | None:
    parsed = _parse_dt(value)
    return parsed.replace(tzinfo=None) if parsed else None


def _source_iso(value: Any) -> str | None:
    parsed = _parse_dt(value)
    return parsed.isoformat() if parsed else None


def _source_order(created_at: Any, event_id: Any) -> tuple[float, str]:
    parsed = _parse_dt(created_at)
    try:
        tie = f"{int(event_id):020d}"
    except (TypeError, ValueError):
        tie = str(event_id or "")
    return (parsed.timestamp() if parsed else float("-inf"), tie)


def _card_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]), "user_id": str(row["user_id"]), "category": row["category"],
        "key": row.get("memory_key", row.get("key")), "version": int(row["version"]),
        "value_json": _json_loads_any(row["value_json"]), "confidence": float(row["confidence"]),
        "source_session_id": row["source_session_id"], "source_event_id": str(row["source_event_id"]),
        "source_seq": int(row["source_seq"]) if row.get("source_seq") is not None else None,
        "source_created_at": _source_iso(row.get("source_created_at")),
        "valid_from": _source_iso(row.get("valid_from")), "valid_to": _source_iso(row.get("valid_to")),
        "sensitivity": row.get("sensitivity", "normal"), "importance": float(row.get("importance") or 0.0),
        "source_refs": _json_loads_list(row.get("source_refs_json")) or [_source_ref_from_row(row)],
        "updated_at": _source_iso(row.get("updated_at")),
    }


def _episode_from_row(row: dict[str, Any]) -> dict[str, Any]:
    value = _json_loads_any(row.get("summary_json", row.get("value_json", "{}")))
    refs = _json_loads_list(row.get("source_refs_json"))
    episode = {
        "id": str(row["id"]), "user_id": str(row["user_id"]),
        "source_session_id": row["source_session_id"], "source_event_id": str(row["source_event_id"]),
        "source_seq": int(row.get("live_source_seq", row.get("source_seq") or 0)),
        "source_created_at": _source_iso(row.get("live_source_created_at") or row.get("source_created_at") or row.get("created_at")),
        "value_json": value, "confidence": float(row["confidence"]),
        "importance": float(row.get("importance") or 0.0), "sensitivity": row.get("sensitivity", "normal"),
        "episode_type": row.get("episode_type", "source"), "source_refs": refs,
        "created_at": _source_iso(row.get("created_at")), "historical": True, "authoritative": False,
    }
    return episode


def _candidate_from_row(row: dict[str, Any]) -> MemoryCandidate:
    return MemoryCandidate(
        kind=CandidateKind(row["kind"]), user_id=str(row["user_id"]), category=row["category"],
        key=row["memory_key"], value_json=_json_loads_any(row["value_json"]),
        confidence=float(row["confidence"]), source_session_id=row["source_session_id"],
        source_event_id=str(row["source_event_id"]), sensitivity=row.get("sensitivity", "normal"),
        reason=row.get("reason") or "explicit_user_statement", eligible=bool(row.get("eligible", 1)),
        source_seq=int(row["source_seq"]) if row.get("source_seq") is not None else None,
        source_created_at=row.get("source_created_at"),
    )


def _profile_importance(candidate: MemoryCandidate) -> float:
    base = {"response_language": 0.8, "response_style": 0.75, "tone": 0.65, "preferred_name": 0.7}.get(candidate.key, 0.1)
    return round(min(1.0, max(0.0, base * candidate.confidence)), 4)


def _episode_importance(candidate: MemoryCandidate) -> float:
    topics = candidate.value_json.get("topics", [])
    return round(min(1.0, max(0.0, candidate.confidence * 0.6 + min(len(topics), 3) * 0.06)), 4)


def _source_ref(candidate: MemoryCandidate) -> dict[str, Any]:
    return {"session_id": candidate.source_session_id, "event_id": candidate.source_event_id, "seq": candidate.source_seq}


def _source_ref_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {"session_id": row["source_session_id"], "event_id": str(row["source_event_id"]), "seq": row.get("source_seq")}


def _source_ref_from_episode(episode: dict[str, Any]) -> dict[str, Any]:
    return {"session_id": episode["source_session_id"], "event_id": str(episode["source_event_id"]), "seq": episode.get("source_seq")}


def _unique_refs(refs) -> list[dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for ref in refs:
        if not isinstance(ref, dict) or not ref.get("session_id") or not ref.get("event_id"):
            continue
        result[(str(ref["session_id"]), str(ref["event_id"]))] = {
            "session_id": str(ref["session_id"]), "event_id": str(ref["event_id"]),
            "seq": int(ref["seq"]) if ref.get("seq") is not None else None,
        }
    return list(result.values())


def _episode_dedup_key(value: Any, fallback_event_id: Any = None) -> str:
    if not isinstance(value, dict):
        return _canonical_json({"value": value, "source_event_id": str(fallback_event_id or "")})
    quote = value.get("historical_quote")
    if not isinstance(quote, str) or not quote:
        # Old generic summaries cannot prove two source statements are identical.
        # Keep them distinct rather than destructively merging unrelated cases.
        return _canonical_json({
            "legacy_summary": value.get("summary", ""),
            "topics": sorted(value.get("topics", [])),
            "source_event_id": str(fallback_event_id or ""),
        })
    return _canonical_json({
        "historical_quote": quote,
        "topics": sorted(value.get("topics", [])),
        "structured_refs": value.get("structured_refs", {}),
    })


def _bounded_historical_quote(
    text: str, *, max_chars: int, identifier_patterns: tuple[re.Pattern[str], ...]
) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    boundary = text.rfind(" ", 0, max_chars + 1)
    if boundary < max_chars // 2:
        boundary = max_chars
    for pattern in identifier_patterns:
        for match in pattern.finditer(text):
            if match.start() < boundary < match.end():
                boundary = match.start()
    return text[:boundary].rstrip(), True


def _unique_text_matches(values) -> list[str]:
    output: list[str] = []
    for value in values:
        if isinstance(value, str) and value and value not in output:
            output.append(value)
    return output


def _is_strict_json_value(value: Any) -> bool:
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_strict_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(type(key) is str and _is_strict_json_value(item) for key, item in value.items())
    return False


def _semantic_text(value: Any) -> str:
    if isinstance(value, dict):
        ignored = {"source", "source_readonly_hint", "not_authoritative", "requires_live_tool_lookup",
                   "source_event_id", "session_id", "confidence", "importance", "created_at",
                   "derived_abstraction"}
        return " ".join(_semantic_text(item) for key, item in value.items() if key not in ignored)
    if isinstance(value, (list, tuple, set)):
        return " ".join(_semantic_text(item) for item in value)
    return str(value) if value is not None else ""


_STOP_WORDS = {
    "the", "a", "an", "and", "or", "to", "for", "of", "in", "on", "with", "is", "it", "my", "i",
    "me", "this", "that", "what", "when", "where", "how", "do", "does", "can", "you", "please",
    "的", "了", "吗", "呢", "我", "你", "他", "她", "它", "是", "在", "请", "一下", "什么",
}


def _search_terms(terms: list[str]) -> list[str]:
    normalized: list[str] = []
    for raw in terms:
        term = str(raw).lower().strip()
        if not term or len(term) > 64 or not re.fullmatch(r"[a-z0-9_\u4e00-\u9fff]+", term):
            continue
        if term not in normalized:
            normalized.append(term)
        if len(normalized) >= 24:
            break
    return normalized


def _tokens(text: str) -> list[str]:
    normalized = (text or "").lower()
    words = re.findall(r"[a-z0-9_]+", normalized)
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", normalized)
    cjk_bigrams = [normalized[index:index + 2] for index in range(max(len(normalized) - 1, 0))]
    cjk_bigrams = [item for item in cjk_bigrams if re.search(r"[\u4e00-\u9fff]", item)]
    return [item for item in words + cjk_chars + cjk_bigrams if item not in _STOP_WORDS and len(item) > 1 or item in {"a", "i"}]


def _score(query_terms: set[str], text: str) -> float:
    body_terms = set(_tokens(text))
    if not query_terms or not body_terms:
        return 0.0
    return len(query_terms & body_terms) / len(query_terms)
