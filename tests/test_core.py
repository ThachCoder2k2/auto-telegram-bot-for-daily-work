"""Focused unit tests for the reliability + config + rendering upgrades."""

from __future__ import annotations

import pytest

from daily_intel_bot import briefing
from daily_intel_bot.briefing import BriefingNewsItem
from daily_intel_bot.config import Settings
from daily_intel_bot.gemini_client import _gemini_schema
from daily_intel_bot.obs import with_retries


# --- config ---------------------------------------------------------------

def test_csv_env_parsed_to_tuple(monkeypatch):
    monkeypatch.setenv("ENABLED_CATEGORIES", "web_tech, gaming ,hardware")
    settings = Settings()
    assert settings.enabled_categories == ("web_tech", "gaming", "hardware")


def test_provider_normalized(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "  GEMINI ")
    assert Settings().ai_provider == "gemini"


def test_spice_level_clamped(monkeypatch):
    monkeypatch.setenv("BOT_PERSONA_SPICE_LEVEL", "9")
    assert Settings().bot_persona_spice_level == 3


def test_bad_int_fails_loudly(monkeypatch):
    monkeypatch.setenv("NEWS_ITEMS_PER_CATEGORY", "not-a-number")
    with pytest.raises(Exception):
        Settings()


def test_default_paths_filled():
    settings = Settings()
    assert settings.state_db_path.endswith("daily_intel.db")
    assert settings.briefing_state_path.endswith("briefing_state.json")


# --- obs.with_retries -----------------------------------------------------

def test_with_retries_succeeds_after_failures():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return "ok"

    assert with_retries(flaky, attempts=3, backoff=0) == "ok"
    assert calls["n"] == 3


def test_with_retries_reraises_last():
    def always_fail():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        with_retries(always_fail, attempts=2, backoff=0)


# --- gemini schema translation -------------------------------------------

def test_gemini_schema_uppercases_types_and_drops_additional_properties():
    schema = _gemini_schema()
    assert schema["type"] == "OBJECT"
    assert "additionalProperties" not in schema
    assert "propertyOrdering" in schema
    # nested array items keep the dialect
    vocab = schema["properties"]["vocabulary"]
    assert vocab["type"] == "ARRAY"
    assert vocab["items"]["type"] == "OBJECT"
    assert "additionalProperties" not in vocab["items"]


# --- rendering helpers ----------------------------------------------------

def test_shorten_word_boundary_no_midword_cut():
    out = briefing._shorten("charging up to $10,000 dollars today", 20)
    assert out.endswith("…")
    assert "$10,00" not in out  # never cut mid-number
    assert len(out) <= 20


def test_shorten_keeps_short_text():
    assert briefing._shorten("short", 20) == "short"


def test_impact_bar_segments():
    assert briefing._impact_bar(8).startswith("▰▰▰▰▱")
    assert briefing._impact_bar(8).endswith("8/10")
    assert briefing._impact_bar(0) == "▱▱▱▱▱ 0/10"


def test_clean_summary_drops_boilerplate():
    assert briefing._clean_summary("Enter your email to subscribe now") == ""
    assert briefing._clean_summary("A real article summary.") == "A real article summary."


def _item(cat, score):
    return BriefingNewsItem(
        category=cat,
        headline=f"{cat} headline",
        impact_score=score,
        url="http://example.com",
        source="example.com",
        summary="",
    )


def test_top_pick_returns_highest_impact():
    news = {
        "web_tech": [_item("web_tech", 6), _item("web_tech", 4)],
        "gaming": [_item("gaming", 9)],
    }
    pick = briefing._top_pick(news)
    assert pick is not None
    assert pick.impact_score == 9
    assert pick.category == "gaming"


def test_top_pick_ignores_noise():
    news = {"web_tech": [_item("web_tech", 1)]}
    assert briefing._top_pick(news) is None
