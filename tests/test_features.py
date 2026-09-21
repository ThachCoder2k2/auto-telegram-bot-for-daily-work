"""Tests for the two-way command loop, ranking memory and spaced repetition."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.error import HTTPError

import pytest

from daily_intel_bot import briefing, commands, poller, telegram_updates
from daily_intel_bot.briefing import BriefingNewsItem, FeedMemory
from daily_intel_bot.commands import handle_command
from daily_intel_bot.config import Settings
from daily_intel_bot.obs import is_transient_http_error, with_retries
from daily_intel_bot.state_store import StateStore, current_streak
from daily_intel_bot.telegram_updates import _is_authorized, _parse_message


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("BRIEFING_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    monkeypatch.setenv("PENDING_TASKS", "First task,Second task")
    return Settings()


def _news(
    headline: str,
    *,
    category: str = "web_tech",
    url: str = "https://example.com/story",
    tavily_score: float = 0.0,
    hn_points: int = 0,
    published_at: datetime | None = None,
) -> BriefingNewsItem:
    return BriefingNewsItem(
        category=category,
        headline=headline,
        impact_score=0,
        url=url,
        source="example.com",
        summary="",
        tavily_score=tavily_score,
        hn_points=hn_points,
        published_at=published_at,
    )


# --- streak ---------------------------------------------------------------


def test_streak_counts_consecutive_days():
    today = date(2026, 9, 21)
    done = {"2026-09-21", "2026-09-20", "2026-09-19"}
    assert current_streak(done, today) == 3


def test_streak_survives_today_not_done_yet():
    """A day still in progress is not a broken streak."""
    today = date(2026, 9, 21)
    done = {"2026-09-20", "2026-09-19"}
    assert current_streak(done, today) == 2


def test_streak_breaks_on_gap():
    today = date(2026, 9, 21)
    done = {"2026-09-20", "2026-09-18"}
    assert current_streak(done, today) == 1


def test_streak_zero_when_nothing_logged():
    assert current_streak(set(), date(2026, 9, 21)) == 0


# --- command flow ---------------------------------------------------------


def test_done_rotates_task_and_records_streak(settings):
    result = handle_command(settings, "/done shipped the prototype")
    assert "First task" in result.reply
    assert "Second task" in result.reply

    state = briefing.load_briefing_state(settings)
    assert state.tasks_remaining == ["Second task"]

    store = StateStore(settings.state_db_path)
    assert store.task_event_counts()["done"] == 1


def test_completed_tasks_are_not_resurrected_from_env(settings):
    """An emptied list must stay empty instead of reseeding from PENDING_TASKS."""
    handle_command(settings, "/done")
    handle_command(settings, "/done")
    assert briefing.load_briefing_state(settings).tasks_remaining == []

    # A later write must not bring the finished tasks back.
    briefing.persist_briefing_state(settings)
    assert briefing.load_briefing_state(settings).tasks_remaining == []
    assert handle_command(settings, "/done").reply.startswith("📭")


def test_task_command_puts_new_work_on_top(settings):
    handle_command(settings, "/task Build the fear meter")
    assert briefing.load_briefing_state(settings).tasks_remaining[0] == (
        "Build the fear meter"
    )


def test_blocked_records_reason(settings):
    handle_command(settings, "/blocked no art assets")
    assert briefing.load_briefing_state(settings).blocked_reason == "no art assets"


def test_bare_done_alias_is_accepted(settings):
    """The digest tells users to reply "done", not "/done"."""
    assert handle_command(settings, "done").reply.startswith("✅")


def test_free_text_is_filed_as_a_note(settings):
    handle_command(settings, "stuck on tilemap collisions")
    store = StateStore(settings.state_db_path)
    assert store.task_event_counts()["note"] == 1


def test_digest_command_requests_a_send(settings):
    assert handle_command(settings, "/digest").action == "send_digest"


def test_unknown_command_falls_back_to_help(settings):
    assert "Commands" in handle_command(settings, "/nope").reply


def test_handler_failure_is_reported_not_raised(settings, monkeypatch):
    """A broken handler must answer the user, not kill the poll loop."""

    def _boom(_settings):
        raise OSError("disk gone")

    monkeypatch.setattr(commands, "load_briefing_state", _boom)
    result = handle_command(settings, "/status")
    assert "failed" in result.reply
    assert "OSError" in result.reply


# --- mutes and feedback ---------------------------------------------------


def test_skip_classifies_domain_vs_keyword(settings):
    handle_command(settings, "/skip nbcnews.com")
    handle_command(settings, "/skip crypto")
    assert set(StateStore(settings.state_db_path).list_mutes()) == {
        ("domain", "nbcnews.com"),
        ("keyword", "crypto"),
    }


def test_muted_domain_and_keyword_are_filtered():
    memory = FeedMemory(
        recent_keys=set(),
        mutes=[("domain", "nbcnews.com"), ("keyword", "crypto")],
        topic_feedback={},
        domain_counts={},
    )
    assert memory.is_muted("Anything", "https://www.nbcnews.com/story")
    assert memory.is_muted("Crypto rally continues", "https://example.com/a")
    assert not memory.is_muted("Godot 4.5 released", "https://godotengine.org/a")


def test_unmute_removes_the_entry(settings):
    handle_command(settings, "/skip nbcnews.com")
    handle_command(settings, "/unmute nbcnews.com")
    assert StateStore(settings.state_db_path).list_mutes() == []


def test_topic_feedback_is_clamped(settings):
    store = StateStore(settings.state_db_path)
    for _ in range(20):
        store.bump_topic_feedback("gaming", 0.5)
    assert store.topic_feedback()["gaming"] == 3.0


# --- ranking --------------------------------------------------------------


def test_tavily_scores_are_normalised_within_a_response():
    """Tavily returns ~0.01-0.05, so only the in-response ordering is usable."""

    class _Result:
        def __init__(self, score):
            self.score = score

    scores = briefing._normalized_tavily_scores(
        [_Result(0.035), _Result(0.016), _Result(0.015)]
    )
    assert scores[0] == pytest.approx(1.0)
    assert scores[2] == pytest.approx(0.0)
    assert scores[0] > scores[1] > scores[2]


def test_flat_tavily_response_is_neutral():
    class _Result:
        score = 0.02

    scores = briefing._normalized_tavily_scores([_Result(), _Result()])
    assert scores == [0.5, 0.5]


def test_impact_score_separates_strong_from_weak():
    memory = FeedMemory.empty()
    fresh_and_popular = _news(
        "Godot 4.5 ships new tilemap",
        tavily_score=1.0,
        hn_points=400,
        published_at=datetime.now(timezone.utc),
    )
    stale_and_ignored = _news(
        "Some wire copy",
        tavily_score=0.0,
        published_at=datetime.now(timezone.utc) - timedelta(days=5),
    )
    strong = briefing._impact_score(fresh_and_popular, ("godot",), memory, 1)
    weak = briefing._impact_score(stale_and_ignored, ("godot",), memory, 1)
    assert strong > weak
    assert strong >= 8


def test_focus_terms_boost_relevant_items():
    plain = FeedMemory.empty()
    focused = FeedMemory(
        recent_keys=set(),
        mutes=[],
        topic_feedback={},
        domain_counts={},
        focus_terms=("godot", "horror"),
    )
    item = _news("Godot physics rewrite lands", url="https://example.com/godot")
    assert briefing._impact_score(item, (), focused, 1) > briefing._impact_score(
        item, (), plain, 1
    )


def test_source_fatigue_damps_repeated_domains():
    item = _news("A story", url="https://cnet.com/a")
    quiet = FeedMemory.empty()
    loud = FeedMemory(
        recent_keys=set(),
        mutes=[],
        topic_feedback={},
        domain_counts={"cnet.com": 6},
    )
    assert briefing._impact_score(item, (), loud, 1) < briefing._impact_score(
        item, (), quiet, 1
    )


def test_clustering_matches_differently_worded_headlines():
    """Outlets never agree word for word, so exact fingerprints miss duplicates."""
    items = [
        _news("Apple M5 Ultra benchmarks leak - MacRumors"),
        _news("M5 Ultra Benchmarks Leak | Reuters"),
        _news("Godot 4.5 adds a new tilemap editor"),
    ]
    clusters = briefing._cluster_stories(items)
    assert clusters[0] == clusters[1]
    assert clusters[2] != clusters[0]


def test_corroboration_counts_distinct_domains():
    items = [
        _news("M5 Ultra benchmarks leak", url="https://macrumors.com/a"),
        _news("M5 Ultra Benchmarks Leak - Reuters", url="https://reuters.com/b"),
        _news("M5 Ultra benchmarks leak", url="https://macrumors.com/c"),
    ]
    clusters = briefing._cluster_stories(items)
    counts = briefing._corroboration_counts(items, clusters)
    assert counts[clusters[0]] == 2  # two domains, not three articles


def test_duplicate_story_is_shown_once(settings):
    spec = briefing.CATEGORY_SPECS[0]
    items = [
        _news("Rust 1.90 released with faster compiler", url="https://a.com/x"),
        _news("Rust 1.90 Released With Faster Compiler - InfoQ", url="https://b.com/y"),
    ]
    ranked = briefing._rank_category(settings, spec, items, FeedMemory.empty())
    assert len(ranked) == 1
    assert ranked[0].corroboration == 2


def test_rank_category_drops_recently_sent_items(settings):
    spec = briefing.CATEGORY_SPECS[0]
    item = _news("Godot 4.5 ships", url="https://godotengine.org/news")
    key = briefing._make_key(item.category, item.url, item.headline)
    memory = FeedMemory(
        recent_keys={key},
        mutes=[],
        topic_feedback={},
        domain_counts={},
    )
    assert briefing._rank_category(settings, spec, [item], memory) == []


def test_market_noise_stays_out_of_web_tech():
    spec = briefing.CATEGORY_SPECS[0]
    assert not briefing._is_category_relevant(
        spec,
        "Stocks advance after Bessent says talks with China were successful",
        "",
        "https://example.com/markets",
    )
    assert briefing._is_category_relevant(
        spec,
        "Rust 1.90 released with faster compiler",
        "",
        "https://example.com/rust",
    )


# --- vocabulary -----------------------------------------------------------


def test_vocabulary_round_trip_and_scheduling(settings):
    store = StateStore(settings.state_db_path)
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    added = store.record_vocabulary([("Pervasive", "lan tỏa", "An example.")], now=now)
    assert added == 1

    assert store.due_vocabulary(now=now) == []  # box 1 is due tomorrow
    due = store.due_vocabulary(now=now + timedelta(days=1))
    assert [entry.word for entry in due] == ["pervasive"]


def test_correct_review_promotes_and_delays(settings):
    store = StateStore(settings.state_db_path)
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    store.record_vocabulary([("resilience", "khả năng phục hồi", "x")], now=now)
    entry = store.review_vocabulary("resilience", correct=True, now=now)
    assert entry is not None and entry.box == 2
    # Box 2 waits two days, so it is not due the next morning.
    assert store.due_vocabulary(now=now + timedelta(days=1)) == []


def test_missed_review_resets_to_box_one(settings):
    store = StateStore(settings.state_db_path)
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    store.record_vocabulary([("iteration", "lặp lại", "x")], now=now)
    store.review_vocabulary("iteration", correct=True, now=now)
    entry = store.review_vocabulary("iteration", correct=False, now=now)
    assert entry is not None
    assert entry.box == 1
    assert entry.wrong == 1


def test_reviewing_unknown_word_returns_none(settings):
    assert StateStore(settings.state_db_path).review_vocabulary("nope", True) is None


def test_duplicate_words_keep_their_schedule(settings):
    store = StateStore(settings.state_db_path)
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    store.record_vocabulary([("constraint", "ràng buộc", "a")], now=now)
    store.review_vocabulary("constraint", correct=True, now=now)
    assert store.record_vocabulary([("constraint", "ràng buộc", "b")], now=now) == 0
    assert store.vocabulary_stats() == (1, 0)


def test_quiz_and_scoring_commands(settings):
    store = StateStore(settings.state_db_path)
    store.record_vocabulary(
        [("guardrails", "rào chắn", "x")],
        now=datetime.now(timezone.utc) - timedelta(days=2),
    )
    assert "guardrails" in handle_command(settings, "/quiz").reply
    assert handle_command(settings, "/got guardrails").reply.startswith("✅")


# --- trends ---------------------------------------------------------------


def test_recurring_terms_need_multiple_headlines(settings):
    store = StateStore(settings.state_db_path)
    store._connect().close()
    with store._connect() as conn:
        for index in range(3):
            conn.execute(
                "INSERT INTO sent_items VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"key{index}",
                    "src",
                    "web_tech",
                    "Regulation tightens for agents",
                    f"https://example.com/{index}",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        conn.commit()
    terms = dict(store.recurring_terms(min_hits=3))
    assert terms.get("regulation") == 3
    # A word repeated inside one headline must not create a trend on its own.
    assert "agents" in terms


# --- retry policy ---------------------------------------------------------


def test_transient_errors_are_retryable():
    """Observed live: Gemini answered 503 and the brief lost its AI sections."""
    assert is_transient_http_error(HTTPError("u", 503, "busy", {}, None))
    assert is_transient_http_error(HTTPError("u", 429, "slow down", {}, None))
    assert is_transient_http_error(TimeoutError())


def test_client_errors_are_not_retried():
    assert not is_transient_http_error(HTTPError("u", 401, "bad key", {}, None))
    assert not is_transient_http_error(HTTPError("u", 400, "bad request", {}, None))


def test_non_retryable_failure_raises_immediately():
    calls = {"n": 0}

    def _fail():
        calls["n"] += 1
        raise HTTPError("u", 401, "bad key", {}, None)

    with pytest.raises(HTTPError):
        with_retries(
            _fail, attempts=3, backoff=0, should_retry=is_transient_http_error
        )
    assert calls["n"] == 1  # no pointless retries on a bad key


def test_transient_failure_is_retried_then_succeeds():
    calls = {"n": 0}

    def _flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise HTTPError("u", 503, "busy", {}, None)
        return "ok"

    assert (
        with_retries(
            _flaky, attempts=3, backoff=0, should_retry=is_transient_http_error
        )
        == "ok"
    )
    assert calls["n"] == 3


# --- inbound parsing ------------------------------------------------------


def test_parse_message_extracts_text_and_chat():
    command = _parse_message(
        {
            "update_id": 7,
            "message": {
                "text": "  /done  ",
                "chat": {"id": 555},
                "from": {"username": "andru"},
            },
        }
    )
    assert command is not None
    assert command.text == "/done"
    assert command.chat_id == "555"
    assert command.from_user == "andru"


def test_parse_message_ignores_non_text_updates():
    assert _parse_message({"update_id": 1, "message": {"chat": {"id": 555}}}) is None
    assert _parse_message({"update_id": 2}) is None


def test_only_the_configured_chat_is_authorized(settings):
    assert _is_authorized(settings, "555")
    assert not _is_authorized(settings, "999")


def test_unset_chat_id_authorizes_nobody(monkeypatch):
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert not _is_authorized(Settings(), "555")


def test_offset_advances_past_updates_we_skip(settings, monkeypatch):
    """A non-text or foreign message must not wedge the poll loop forever."""
    payload = {
        "ok": True,
        "result": [
            {"update_id": 10, "message": {"chat": {"id": 999}, "text": "/done"}},
            {"update_id": 11, "message": {"chat": {"id": 555}, "photo": []}},
        ],
    }
    monkeypatch.setattr(
        telegram_updates.request, "urlopen", _fake_urlopen(payload)
    )
    commands_found, offset = telegram_updates.fetch_updates(settings, 0, timeout=0)
    assert commands_found == []
    assert offset == 12


def test_first_start_skips_the_backlog(settings, monkeypatch):
    """Telegram keeps 24h of updates; replaying them could close stale tasks."""
    monkeypatch.setattr(poller, "prime_offset", lambda _s: 42)
    store = StateStore(settings.state_db_path)
    assert poller._load_offset(settings, store) == 42
    # Subsequent starts resume from the stored value without re-priming.
    monkeypatch.setattr(
        poller, "prime_offset", lambda _s: pytest.fail("should not re-prime")
    )
    assert poller._load_offset(settings, store) == 42


def _fake_urlopen(payload: dict):
    import json
    from contextlib import contextmanager

    @contextmanager
    def _open(*_args, **_kwargs):
        class _Response:
            def read(self):
                return json.dumps(payload).encode("utf-8")

        yield _Response()

    return _open


# --- timezone correctness -------------------------------------------------


def test_early_morning_done_does_not_break_the_streak(settings, monkeypatch):
    """In UTC+7, /done before 07:00 used to land on yesterday in UTC.

    The streak then compared that against today's local date and read as
    broken, punishing the user for finishing early.
    """
    import daily_intel_bot.commands as commands_module

    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    early = datetime(2026, 9, 22, 6, 30, tzinfo=tz)
    monkeypatch.setattr(commands_module, "_now", lambda _s: early)

    handle_command(settings, "/done")

    store = StateStore(settings.state_db_path)
    assert "2026-09-22" in store.done_days(today=early.date())
    assert current_streak(store.done_days(today=early.date()), early.date()) == 1


def test_task_events_use_the_local_calendar_day():
    import tempfile

    store = StateStore(str(Path(tempfile.mkdtemp()) / "t.db"))
    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    store.record_task_event("t", "done", "", datetime(2026, 9, 22, 1, 0, tzinfo=tz))
    # 01:00 ICT is still 2026-09-21 in UTC; the streak cares about local days.
    assert store.done_days(today=date(2026, 9, 22)) == {"2026-09-22"}


def test_vocabulary_due_check_survives_a_local_timezone_now():
    """SQL compares ISO strings, so a +07:00 parameter must be normalised."""
    import tempfile

    store = StateStore(str(Path(tempfile.mkdtemp()) / "t.db"))
    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    created = datetime(2026, 9, 22, 7, 30, tzinfo=tz)  # 00:30 UTC
    store.record_vocabulary([("pervasive", "lan toa", "x")], now=created)

    # Local clock a little before the word is due: must not be returned.
    too_early = datetime(2026, 9, 23, 6, 0, tzinfo=tz)  # 23:00 UTC on the 22nd
    assert store.due_vocabulary(now=too_early) == []

    # Once the interval really has elapsed, it is.
    later = datetime(2026, 9, 23, 9, 0, tzinfo=tz)  # 02:00 UTC on the 23rd
    assert [entry.word for entry in store.due_vocabulary(now=later)] == ["pervasive"]


# --- retry backoff --------------------------------------------------------


def test_rate_limit_backs_off_far_harder_than_a_server_error():
    """429 is a quota, not a blip; retrying on a 503 rhythm just hammers it."""
    from daily_intel_bot.obs import _delay_for

    server_error = HTTPError("u", 503, "busy", {}, None)
    rate_limited = HTTPError("u", 429, "slow down", {}, None)
    assert _delay_for(rate_limited, backoff=2.0, attempt=1) > _delay_for(
        server_error, backoff=2.0, attempt=1
    )


def test_retry_after_header_is_honoured():
    from daily_intel_bot.obs import _delay_for

    exc = HTTPError("u", 429, "slow down", {"Retry-After": "7"}, None)
    assert _delay_for(exc, backoff=2.0, attempt=1) == 7.0


def test_retry_delay_is_capped():
    from daily_intel_bot.obs import MAX_RETRY_SLEEP_SECONDS, _delay_for

    exc = HTTPError("u", 429, "slow down", {"Retry-After": "99999"}, None)
    assert _delay_for(exc, backoff=2.0, attempt=1) == MAX_RETRY_SLEEP_SECONDS


def test_garbage_retry_after_falls_back_to_the_default_backoff():
    from daily_intel_bot.obs import RATE_LIMIT_BACKOFF_SECONDS, _delay_for

    exc = HTTPError("u", 429, "slow down", {"Retry-After": "soon"}, None)
    assert _delay_for(exc, backoff=2.0, attempt=1) == RATE_LIMIT_BACKOFF_SECONDS
