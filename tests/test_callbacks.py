"""Tests for tap-to-reply buttons.

Three days of nudges produced 160 messages and zero replies, because every
reply path needed a typed command. These cover the tap path that replaces it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from daily_intel_bot import callbacks, notion_tasks
from daily_intel_bot.callbacks import (
    ACTION_DONE,
    ACTION_SNOOZE,
    ACTION_STOP,
    button_row,
    parse_action,
    strip_handled_row,
)
from daily_intel_bot.config import Settings
from daily_intel_bot.notion_client import NotionError, NotionSchema, NotionTask
from daily_intel_bot.reminders import ReminderWindow, build_keyboard, due_reminders
from daily_intel_bot.state_store import StateStore
from daily_intel_bot.telegram_updates import _parse_callback

UTC = timezone.utc
NOW = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)
PAGE = "3e2207ad-5a6f-808d-87e9-ee8d555b80bb"


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("BRIEFING_STATE_PATH", str(tmp_path / "s.json"))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    monkeypatch.setenv("NOTION_ENABLED", "true")
    monkeypatch.setenv("NOTION_TOKEN", "ntn_x")
    monkeypatch.setenv("NOTION_DATABASE_ID", "db")
    monkeypatch.setenv("NOTION_WRITE_ENABLED", "true")
    monkeypatch.setenv("TIMEZONE", "Asia/Ho_Chi_Minh")
    return Settings()


def _task(page_id=PAGE, title="Nâng cấp uploader") -> NotionTask:
    return NotionTask(
        page_id=page_id,
        title=title,
        status="Not Started",
        priority="",
        estimated_time="",
        reminder=True,
        reminder_frequency="Every hour",
        last_reminded=None,
        url="https://notion.so/x",
    )


def _board(monkeypatch, tasks=None):
    board = notion_tasks.Board(
        schema=NotionSchema(title="Task", status="Status", reminder="Reminder"),
        tasks=tasks if tasks is not None else [_task()],
    )
    monkeypatch.setattr(callbacks.notion_tasks, "load_board", lambda *a: board)
    return board


# --- callback_data --------------------------------------------------------


def test_callback_data_fits_telegrams_64_byte_cap():
    """A Notion uuid plus a prefix must stay inside the limit."""
    for button in button_row(PAGE, 3):
        assert len(button["callback_data"].encode("utf-8")) <= 64


def test_button_row_offers_the_three_useful_replies():
    actions = [parse_action(b["callback_data"])[0] for b in button_row(PAGE, 3)]
    assert actions == [ACTION_DONE, ACTION_SNOOZE, ACTION_STOP]


def test_snooze_button_shows_the_configured_hours():
    assert "5h" in button_row(PAGE, 5)[1]["text"]


def test_parse_action_splits_on_the_first_colon_only():
    assert parse_action("done:abc:def") == ("done", "abc:def")


def test_keyboard_has_one_row_per_task():
    keyboard = build_keyboard([_task("a"), _task("b")], 3)
    assert len(keyboard["inline_keyboard"]) == 2


# --- handling -------------------------------------------------------------


def test_done_marks_the_task_and_clears_its_nag_history(settings, monkeypatch):
    _board(monkeypatch)
    marked = {}
    monkeypatch.setattr(
        callbacks.notion_tasks,
        "complete_task",
        lambda s, schema, pid: marked.setdefault("page", pid),
    )
    store = StateStore(settings.state_db_path)
    store.record_reminder(PAGE, "Nâng cấp uploader")

    result = callbacks.handle(settings, f"{ACTION_DONE}:{PAGE}")
    assert marked["page"] == PAGE
    assert "Nâng cấp uploader" in result.note
    assert result.handled_page_id == PAGE
    assert PAGE not in store.reminder_counts()


def test_snooze_silences_the_task_without_touching_notion(settings, monkeypatch):
    """A snooze must not write a future date into the user's own column."""
    touched = []
    monkeypatch.setattr(
        callbacks.notion_tasks,
        "load_board",
        lambda *a: touched.append("load") or _board(monkeypatch),
    )
    result = callbacks.handle(settings, f"{ACTION_SNOOZE}:{PAGE}")
    assert result.handled_page_id == PAGE
    assert touched == []  # no Notion call at all

    store = StateStore(settings.state_db_path)
    assert PAGE in store.snoozed_page_ids()


def test_a_snoozed_task_is_skipped_until_the_window_passes(settings):
    store = StateStore(settings.state_db_path)
    store.snooze_task(PAGE, NOW + timedelta(hours=3))
    task = _task()
    always_open = ReminderWindow(0, 0)

    assert due_reminders([task], NOW, always_open, snoozed=store.snoozed_page_ids(NOW)) == []
    later = NOW + timedelta(hours=4)
    assert due_reminders([task], later, always_open, snoozed=store.snoozed_page_ids(later)) == [task]


def test_stop_unticks_the_reminder_rather_than_deleting(settings, monkeypatch):
    _board(monkeypatch)
    calls = {}
    monkeypatch.setattr(
        callbacks.notion_tasks,
        "stop_reminding",
        lambda s, schema, pid: calls.setdefault("page", pid),
    )
    result = callbacks.handle(settings, f"{ACTION_STOP}:{PAGE}")
    assert calls["page"] == PAGE
    assert "reminders off" in result.note


def test_a_notion_failure_still_answers_the_tap(settings, monkeypatch):
    """Telegram spins on the button until the bot replies."""

    def _boom(*_a, **_k):
        raise NotionError("404 from Notion")

    monkeypatch.setattr(callbacks.notion_tasks, "load_board", _boom)
    result = callbacks.handle(settings, f"{ACTION_DONE}:{PAGE}")
    assert "Notion" in result.toast
    assert result.handled_page_id == ""


def test_an_unknown_action_is_rejected_quietly(settings):
    assert callbacks.handle(settings, "bogus:x").toast == "Unrecognised button"
    assert callbacks.handle(settings, "done:").toast == "Unrecognised button"


# --- message amendment ----------------------------------------------------


def test_handled_buttons_are_removed_so_they_cannot_be_tapped_twice():
    keyboard = [button_row("a", 3), button_row("b", 3)]
    remaining = strip_handled_row(keyboard, "a")
    assert len(remaining) == 1
    assert parse_action(remaining[0][0]["callback_data"])[1] == "b"


def test_stripping_an_unknown_page_leaves_the_keyboard_alone():
    keyboard = [button_row("a", 3)]
    assert strip_handled_row(keyboard, "zzz") == keyboard


# --- inbound --------------------------------------------------------------


def test_taps_arrive_as_callbacks_with_their_keyboard():
    event = _parse_callback(
        {
            "update_id": 7,
            "callback_query": {
                "id": "q1",
                "data": f"{ACTION_DONE}:{PAGE}",
                "message": {
                    "message_id": 42,
                    "chat": {"id": 555},
                    "text": "nudge",
                    "reply_markup": {"inline_keyboard": [button_row(PAGE, 3)]},
                },
            },
        }
    )
    assert event is not None
    assert event.message_id == 42
    assert event.data == f"{ACTION_DONE}:{PAGE}"
    assert len(event.keyboard) == 1


def test_a_callback_without_a_message_is_ignored():
    assert _parse_callback({"update_id": 1, "callback_query": {"id": "q", "data": "x"}}) is None


# --- back-off on silence --------------------------------------------------


def test_an_ignored_task_is_nudged_less_often():
    """39 unanswered hourly nudges is evidence hourly is not working."""
    from daily_intel_bot.reminders import backoff_multiplier

    assert backoff_multiplier(0) == 1
    assert backoff_multiplier(9) == 1
    assert backoff_multiplier(10) == 4
    assert backoff_multiplier(25) == 24
    assert backoff_multiplier(39) == 24


def test_backoff_actually_delays_the_next_nudge():
    task = _task()
    object.__setattr__(task, "last_reminded", NOW - timedelta(hours=2))
    always_open = ReminderWindow(0, 0)

    # Untouched task: two hours is plenty for an hourly reminder.
    assert due_reminders([task], NOW, always_open, counts={PAGE: 1}) == [task]
    # Ignored 12 times: the interval has stretched to four hours.
    assert due_reminders([task], NOW, always_open, counts={PAGE: 12}) == []


def test_escalation_fires_once_per_threshold_not_every_time():
    from daily_intel_bot.reminders import is_escalation_point

    assert is_escalation_point(10) is True
    assert is_escalation_point(11) is False
    assert is_escalation_point(25) is True
    assert is_escalation_point(39) is False


def test_escalation_message_asks_a_different_question():
    from daily_intel_bot.reminders import render_escalation

    text = render_escalation(_task(), 39, None)
    assert "39 times" in text
    assert "Too big, no longer needed, or just not now?" in text


def test_escalation_offers_ways_out_not_another_nudge():
    from daily_intel_bot.callbacks import ACTION_LATER, ACTION_SPLIT, escalation_row

    actions = [parse_action(b["callback_data"])[0] for b in escalation_row(PAGE)]
    assert actions == [ACTION_SPLIT, ACTION_LATER, ACTION_STOP]


def test_park_for_a_week_silences_without_touching_notion(settings, monkeypatch):
    from daily_intel_bot.callbacks import ACTION_LATER, LATER_DAYS

    monkeypatch.setattr(
        callbacks.notion_tasks, "load_board", lambda *a: pytest.fail("no Notion call")
    )
    result = callbacks.handle(settings, f"{ACTION_LATER}:{PAGE}")
    assert result.handled_page_id == PAGE

    store = StateStore(settings.state_db_path)
    assert PAGE in store.snoozed_page_ids()
    assert PAGE not in store.snoozed_page_ids(
        datetime.now(timezone.utc) + timedelta(days=LATER_DAYS + 1)
    )


def test_too_big_buys_a_day_and_says_what_to_do(settings, monkeypatch):
    from daily_intel_bot.callbacks import ACTION_SPLIT

    monkeypatch.setattr(
        callbacks.notion_tasks, "load_board", lambda *a: pytest.fail("no Notion call")
    )
    result = callbacks.handle(settings, f"{ACTION_SPLIT}:{PAGE}")
    assert "Split it into a first step" in result.note
    assert PAGE in StateStore(settings.state_db_path).snoozed_page_ids()
