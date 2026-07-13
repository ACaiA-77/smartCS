"""Tests for versioned intent entities and intent-router configuration."""

from __future__ import annotations

import pytest

from api.settings import AppSettings
from memory.working_memory import WorkingMemory


def test_working_memory_exports_only_unexpired_entities_and_overwrites_corrections():
    memory = WorkingMemory()
    memory.merge_entities("s-1", {"product": "iPhone 15", "region": "CN"}, confirmed_turn=1)
    memory.merge_entities("s-1", {"product": "iPhone 16"}, confirmed_turn=3)

    assert memory.get_active_entities("s-1", ttl_turns=2, current_turn=4) == {"product": "iPhone 16"}
    assert memory.get_active_entities("s-1", ttl_turns=2, current_turn=5) == {"product": "iPhone 16"}
    assert memory.get_active_entities("s-1", ttl_turns=2, current_turn=6) == {}


def test_settings_parse_valid_intent_configuration(monkeypatch):
    monkeypatch.setenv("INTENT_CONFIDENCE_THRESHOLD", "0.62")
    monkeypatch.setenv("INTENT_CANDIDATE_MARGIN", "0.18")
    monkeypatch.setenv("INTENT_CONTEXT_TURNS", "4")
    monkeypatch.setenv("INTENT_ENTITY_TTL_TURNS", "7")
    monkeypatch.setenv("INTENT_FORMAT_REPAIR_ENABLED", "false")
    monkeypatch.setenv("INTENT_PROMPT_VERSION", "apple-support-v2")

    settings = AppSettings.from_env()

    assert settings.intent_confidence_threshold == 0.62
    assert settings.intent_candidate_margin == 0.18
    assert settings.intent_context_turns == 4
    assert settings.intent_entity_ttl_turns == 7
    assert settings.intent_format_repair_enabled is False
    assert settings.intent_prompt_version == "apple-support-v2"


@pytest.mark.parametrize(
    ("name", "value"),
    [("INTENT_CONFIDENCE_THRESHOLD", "1.1"), ("INTENT_CANDIDATE_MARGIN", "-0.1"), ("INTENT_CONTEXT_TURNS", "0"), ("INTENT_ENTITY_TTL_TURNS", "0")],
)
def test_settings_reject_invalid_intent_thresholds_and_turns(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError):
        AppSettings.from_env()
