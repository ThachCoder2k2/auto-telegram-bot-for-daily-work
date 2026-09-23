"""Tests for self-reported health."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError

import pytest

from daily_intel_bot import health
from daily_intel_bot.config import Settings
from daily_intel_bot.state_store import StateStore
from daily_intel_bot.telegram_updates import TelegramUnavailable, fetch_updates


UTC = timezone.utc
NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path) -> StateStore:
    return StateStore(str(tmp_path / "t.db"))


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    return Settings()


# --- recording ------------------------------------------------------------


def test_success_and_failure_are_both_counted(store):
    health.record(store, "gemini")
    health.record(store, "gemini", HTTPError("u", 429, "slow", {}, None))
    stats = store.api_health()["gemini"]
    assert stats == {"total": 2, "failed": 1, "rate_limited": 1}


def test_recording_never_raises_into_the_caller(monkeypatch, store):
    """Health must not be able to break the thing it is observing."""

    def _boom(*_a, **_k):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(store, "record_api_call", _boom)
    health.record(store, "telegram")  # must not raise


def test_last_failure_detail_is_kept(store):
    health.record(store, "notion", OSError("Name or service not known"))
    failure = store.last_api_failure("notion")
    assert failure is not None
    assert "Name or service not known" in failure[1]


def test_pruning_drops_old_rows_only(store):
    store.record_api_call("gemini", True, 200, now=NOW - timedelta(days=30))
    store.record_api_call("gemini", True, 200, now=NOW)
    store.prune_api_calls(keep_days=7)
    assert store.api_health(within_hours=24 * 365)["gemini"]["total"] == 1


# --- alerts ---------------------------------------------------------------


def test_a_missing_brief_is_the_headline_alert(store, settings):
    """Two days of briefs went missing in silence; that must not repeat."""
    health.note_digest_sent(store, NOW - timedelta(days=2))
    problems = health.alerts(store, settings, NOW)
    assert any("No daily brief for 2 day" in p for p in problems)


def test_a_recent_brief_raises_no_alert(store, settings):
    health.note_digest_sent(store, NOW - timedelta(hours=3))
    assert health.alerts(store, settings, NOW) == []


def test_never_delivered_is_called_out(store, settings):
    assert any(
        "has ever been delivered" in p for p in health.alerts(store, settings, NOW)
    )


def test_heavy_rate_limiting_names_quota_as_the_cause(store, settings):
    health.note_digest_sent(store, NOW)
    for _ in range(3):
        health.record(store, "gemini", HTTPError("u", 429, "slow", {}, None))
    health.record(store, "gemini")
    assert any("quota is the limit" in p for p in health.alerts(store, settings, NOW))


def test_occasional_rate_limiting_is_not_alarming(store, settings):
    health.note_digest_sent(store, NOW)
    health.record(store, "gemini", HTTPError("u", 429, "slow", {}, None))
    for _ in range(9):
        health.record(store, "gemini")
    assert health.alerts(store, settings, NOW) == []


def test_a_totally_dead_service_is_reported_with_its_reason(store, settings):
    health.note_digest_sent(store, NOW)
    health.record(store, "notion", OSError("Name or service not known"))
    problems = health.alerts(store, settings, NOW)
    assert any("notion failing on every call" in p for p in problems)
    assert any("Name or service not known" in p for p in problems)


# --- report ---------------------------------------------------------------


def test_report_states_delivery_and_per_service_counts(store, settings):
    health.note_digest_sent(store, NOW - timedelta(hours=2))
    health.record(store, "telegram")
    report = health.render_report(store, settings, NOW)
    assert "Daily brief:" in report
    assert "telegram: 1/1 ok" in report
    assert "Nothing wrong" in report


def test_report_escapes_failure_detail(store, settings):
    health.note_digest_sent(store, NOW)
    health.record(store, "notion", OSError("<script> & co"))
    report = health.render_report(store, settings, NOW)
    assert "&lt;script&gt;" in report


# --- the hot-loop guard ---------------------------------------------------


def test_unreachable_telegram_raises_instead_of_looking_idle(settings, monkeypatch):
    """An empty result is indistinguishable from "no new messages".

    Returning one on failure meant the caller looped straight back in, and
    because a failed call returns instantly the poller spun ~1000 times a
    second for the length of the outage.
    """
    import daily_intel_bot.telegram_updates as module

    def _boom(*_a, **_k):
        raise OSError("Name or service not known")

    monkeypatch.setattr(module.request, "urlopen", _boom)
    with pytest.raises(TelegramUnavailable):
        fetch_updates(settings, 0, timeout=0)
