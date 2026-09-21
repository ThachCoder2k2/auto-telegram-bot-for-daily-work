"""Tests for the Notion board reader and the reminder scheduler."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

from zoneinfo import ZoneInfo

import pytest

from daily_intel_bot import notion_tasks
from daily_intel_bot.config import Settings
from daily_intel_bot.notion_client import (
    NoteEntry,
    NotionSchema,
    NotionTask,
    _parse_task,
    detect_notes_schema,
    detect_schema,
)
from daily_intel_bot.nudge_voice import (
    NudgeVoice,
    note_context,
    parse_nudge_voice,
    task_context,
)
from daily_intel_bot.persona import PERSONA_PROFILES
from daily_intel_bot.reminders import (
    NAG_THRESHOLD,
    ReminderWindow,
    describe_gaps,
    due_reminders,
    notes_needing_alert,
    render_note_alert,
    render_note_line,
    upcoming_notes,
    render_reminder_batch,
    sort_tasks,
)
from daily_intel_bot.state_store import StateStore


UTC = timezone.utc
NOW = datetime(2026, 9, 21, 17, tzinfo=UTC)


def _replace_idle(task: NotionTask, days: int) -> NotionTask:
    """Rebuild a task as though Notion last saw an edit ``days`` ago."""
    return dataclasses.replace(
        task,
        edited_at=NOW - timedelta(days=days),
        created_at=NOW - timedelta(days=days + 1),
    )


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("BRIEFING_STATE_PATH", str(tmp_path / "s.json"))
    monkeypatch.setenv("NOTION_ENABLED", "true")
    monkeypatch.setenv("NOTION_TOKEN", "ntn_test")
    monkeypatch.setenv("NOTION_DATABASE_ID", "deadbeef")
    monkeypatch.setenv("NOTION_PROP_STATUS", "")
    monkeypatch.setenv("NOTION_PROP_PRIORITY", "")
    monkeypatch.setenv("NOTION_PROP_LAST_REMINDED", "")
    return Settings()


def _task(
    title="Task",
    *,
    reminder=True,
    frequency="Every hour",
    last=None,
    status="Not Started",
    priority="",
    estimated="",
    page_id="p1",
) -> NotionTask:
    return NotionTask(
        page_id=page_id,
        title=title,
        status=status,
        priority=priority,
        estimated_time=estimated,
        reminder=reminder,
        reminder_frequency=frequency,
        last_reminded=last,
        url="https://notion.so/p1",
    )


# --- schema detection -----------------------------------------------------

# The real board this was built against.
LIVE_PROPERTIES = {
    "Status": {
        "type": "status",
        "status": {
            "options": [
                {"name": "Not Started"},
                {"name": "In Progress"},
                {"name": "Done"},
                {"name": "Pending"},
            ]
        },
    },
    "Reminder Frequency": {"type": "select", "select": {"options": []}},
    "Reminder": {"type": "checkbox"},
    "Priority": {"type": "select", "select": {"options": []}},
    "Estimated Time": {"type": "select", "select": {"options": []}},
    "Last Reminded": {"type": "date"},
    "Notes": {"type": "rich_text"},
    "Task": {"type": "title"},
}


def test_detects_every_column_on_the_live_board(settings):
    schema = detect_schema(LIVE_PROPERTIES, settings)
    assert schema.title == "Task"
    assert schema.status == "Status"
    assert schema.priority == "Priority"
    assert schema.estimated_time == "Estimated Time"
    assert schema.reminder == "Reminder"
    assert schema.reminder_frequency == "Reminder Frequency"
    assert schema.last_reminded == "Last Reminded"
    assert schema.done_option == "Done"
    assert schema.status_is_checkbox is False


def test_env_override_wins_over_detection(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("NOTION_PROP_PRIORITY", "Mức độ")
    schema = detect_schema(LIVE_PROPERTIES, Settings())
    assert schema.priority == "Mức độ"


def test_checkbox_board_is_supported(settings):
    schema = detect_schema(
        {"Name": {"type": "title"}, "Done": {"type": "checkbox"}}, settings
    )
    assert schema.status == "Done"
    assert schema.status_is_checkbox is True


def test_done_option_follows_the_boards_own_wording(settings):
    schema = detect_schema(
        {
            "Name": {"type": "title"},
            "State": {
                "type": "status",
                "status": {"options": [{"name": "Todo"}, {"name": "Completed"}]},
            },
        },
        settings,
    )
    assert schema.done_option == "Completed"


# --- page parsing ---------------------------------------------------------


def test_parse_task_reads_every_property_type():
    schema = NotionSchema(
        title="Task",
        status="Status",
        priority="Priority",
        estimated_time="Estimated Time",
        reminder="Reminder",
        reminder_frequency="Reminder Frequency",
        last_reminded="Last Reminded",
    )
    page = {
        "id": "abc",
        "url": "https://notion.so/abc",
        "properties": {
            "Task": {"type": "title", "title": [{"plain_text": "Build fear meter"}]},
            "Status": {"type": "status", "status": {"name": "In Progress"}},
            "Priority": {"type": "select", "select": {"name": "High"}},
            "Estimated Time": {"type": "select", "select": {"name": "30 min"}},
            "Reminder": {"type": "checkbox", "checkbox": True},
            "Reminder Frequency": {"type": "select", "select": {"name": "Every day"}},
            "Last Reminded": {"type": "date", "date": {"start": "2026-09-20T08:00:00.000Z"}},
        },
    }
    task = _parse_task(page, schema)
    assert task.title == "Build fear meter"
    assert task.priority == "High"
    assert task.reminder is True
    assert task.reminder_frequency == "Every day"
    assert task.last_reminded == datetime(2026, 9, 20, 8, tzinfo=UTC)
    assert not task.is_done


def test_parse_task_tolerates_empty_properties():
    """The live board has rows with every optional column unset."""
    schema = NotionSchema(
        title="Task",
        status="Status",
        priority="Priority",
        reminder="Reminder",
        last_reminded="Last Reminded",
    )
    page = {
        "id": "abc",
        "properties": {
            "Task": {"type": "title", "title": [{"plain_text": "Portfolio"}]},
            "Status": {"type": "status", "status": None},
            "Priority": {"type": "select", "select": None},
            "Reminder": {"type": "checkbox", "checkbox": False},
            "Last Reminded": {"type": "date", "date": None},
        },
    }
    task = _parse_task(page, schema)
    assert task.title == "Portfolio"
    assert task.priority == ""
    assert task.last_reminded is None
    assert task.reminder is False


# --- reminder scheduling --------------------------------------------------


def test_reminder_fires_once_interval_elapsed():
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    stale = _task(last=now - timedelta(hours=2))
    assert due_reminders([stale], now, ReminderWindow(0, 0)) == [stale]


def test_reminder_holds_inside_interval():
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    fresh = _task(last=now - timedelta(minutes=20))
    assert due_reminders([fresh], now, ReminderWindow(0, 0)) == []


def test_never_reminded_task_is_due_immediately():
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    assert due_reminders([_task(last=None)], now, ReminderWindow(0, 0))


def test_once_fires_only_a_single_time():
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    assert due_reminders([_task(frequency="Once", last=None)], now, ReminderWindow(0, 0))
    already = _task(frequency="Once", last=now - timedelta(days=30))
    assert due_reminders([already], now, ReminderWindow(0, 0)) == []


def test_frequency_none_and_unchecked_are_ignored():
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    assert due_reminders([_task(frequency="None")], now, ReminderWindow(0, 0)) == []
    assert due_reminders([_task(reminder=False)], now, ReminderWindow(0, 0)) == []


def test_blank_frequency_falls_back_to_the_default():
    """Ticking Reminder and leaving Frequency blank reads as 'remind me'."""
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    task = _task(frequency="", last=None)
    assert due_reminders([task], now, ReminderWindow(0, 0)) == []
    assert due_reminders(
        [task], now, ReminderWindow(0, 0), default_frequency="Every day"
    ) == [task]


def test_default_frequency_respects_its_own_interval():
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    fresh = _task(frequency="", last=now - timedelta(hours=2))
    assert (
        due_reminders(
            [fresh], now, ReminderWindow(0, 0), default_frequency="Every day"
        )
        == []
    )


def test_explicit_none_still_beats_the_default():
    """An explicit 'None' is a decision; blank is an omission."""
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    opted_out = _task(frequency="None", last=None)
    assert (
        due_reminders(
            [opted_out], now, ReminderWindow(0, 0), default_frequency="Every day"
        )
        == []
    )


def test_unknown_frequency_never_fires():
    """A new option in Notion must not default to hourly spam."""
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    odd = _task(frequency="Every fortnight", last=None)
    assert due_reminders([odd], now, ReminderWindow(0, 0)) == []


def test_done_tasks_are_never_reminded():
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    finished = _task(status="Done", last=None)
    assert due_reminders([finished], now, ReminderWindow(0, 0)) == []


def test_quiet_hours_hold_reminders_until_morning():
    window = ReminderWindow(8, 22)
    overnight = datetime(2026, 9, 21, 3, tzinfo=UTC)
    morning = datetime(2026, 9, 21, 8, tzinfo=UTC)
    task = _task(last=None)
    assert due_reminders([task], overnight, window) == []
    assert due_reminders([task], morning, window) == [task]


def test_window_wrapping_midnight_is_a_union_not_an_empty_range():
    """A night-shift window (22->06) must stay open across midnight."""
    window = ReminderWindow(22, 6)
    assert window.is_open(datetime(2026, 9, 21, 23, tzinfo=UTC))
    assert window.is_open(datetime(2026, 9, 21, 2, tzinfo=UTC))
    assert not window.is_open(datetime(2026, 9, 21, 12, tzinfo=UTC))


def test_hourly_check_does_not_halve_an_hourly_reminder():
    """Check cadence equal to the interval always measures a hair under it.

    Without tolerance an "Every hour" task checked hourly would skip every
    other check and effectively become two-hourly.
    """
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    task = _task(last=now - timedelta(minutes=59, seconds=58))
    assert due_reminders([task], now, ReminderWindow(0, 0)) == []
    tolerance = timedelta(seconds=1800)  # half of an hourly check
    assert due_reminders([task], now, ReminderWindow(0, 0), tolerance) == [task]


def test_tolerance_does_not_fire_a_reminder_far_too_early():
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    task = _task(frequency="Every day", last=now - timedelta(hours=2))
    tolerance = timedelta(seconds=1800)
    assert due_reminders([task], now, ReminderWindow(0, 0), tolerance) == []


def test_reminders_sorted_by_priority():
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    low = _task("low one", priority="Low", last=None, page_id="a")
    high = _task("high one", priority="High", last=None, page_id="b")
    assert [t.title for t in due_reminders([low, high], now, ReminderWindow(0, 0))] == [
        "high one",
        "low one",
    ]


# --- display ordering -----------------------------------------------------


def test_sort_prefers_priority_then_quickest_win():
    tasks = [
        _task("slow high", priority="High", estimated="4+ hours", page_id="a"),
        _task("fast high", priority="High", estimated="15 min", page_id="b"),
        _task("medium", priority="Medium", estimated="5 min", page_id="c"),
    ]
    assert [t.title for t in sort_tasks(tasks)] == ["fast high", "slow high", "medium"]


def test_unsized_tasks_sort_after_sized_ones():
    tasks = [
        _task("unknown effort", priority="High", estimated="", page_id="a"),
        _task("all day", priority="High", estimated="All day", page_id="b"),
    ]
    assert [t.title for t in sort_tasks(tasks)] == ["all day", "unknown effort"]


def test_reminder_message_escapes_user_content():
    task = _task("Fix <script> & hitbox", priority="High")
    rendered = render_reminder_batch([task], NOW, None, {})
    assert "&lt;script&gt; &amp; hitbox" in rendered
    assert "<script>" not in rendered


# --- reminder message -----------------------------------------------------


def test_batch_is_one_message_not_one_per_task():
    """Three identically-worded nudges at once is noise, not three reminders."""
    tasks = [
        _task("Alpha", page_id="a"),
        _task("Beta", page_id="b"),
        _task("Gamma", page_id="c"),
    ]
    rendered = render_reminder_batch(tasks, NOW, None, {})
    for number, title in enumerate(("Alpha", "Beta", "Gamma"), start=1):
        assert title in rendered
        assert f"{number}. " in rendered
    # The call-to-action and framing appear once, not once per task.
    assert rendered.count("/ntasks") == 1
    assert rendered.count("Still open") == 1


def test_persona_voice_is_used():
    persona = PERSONA_PROFILES["rot_maiden"]
    rendered = render_reminder_batch([_task()], NOW, persona, {})
    assert persona.name in rendered
    assert persona.reminder_line in rendered


def test_tone_escalates_once_a_task_is_being_dodged():
    persona = PERSONA_PROFILES["rot_maiden"]
    task = _task(page_id="p1")
    calm = render_reminder_batch([task], NOW, persona, {"p1": 1})
    nagging = render_reminder_batch([task], NOW, persona, {"p1": NAG_THRESHOLD})
    assert persona.reminder_line in calm
    assert persona.nag_line in nagging
    assert persona.nag_line not in calm


def test_per_task_line_carries_effort_age_and_count():
    task = _replace_idle(
        _task("Nâng cấp uploader", page_id="p1", estimated="1 hour"), days=6
    )
    rendered = render_reminder_batch([task], NOW, None, {"p1": 4})
    assert "1 hour" in rendered
    assert "open 7d" in rendered
    assert "nudge #4" in rendered


def test_a_task_with_nothing_to_report_gets_no_detail_line():
    """Three tasks printing three identical grey lines is noise, not detail."""
    fresh = dataclasses.replace(
        _task("Just made", page_id="p1"),
        created_at=NOW,
        edited_at=NOW,
    )
    rendered = render_reminder_batch([fresh], NOW, None, {"p1": 1})
    assert "Just made" in rendered
    assert "<i>" not in rendered.split("Just made", 1)[1].split("⚠️")[0]


# --- gap analysis ---------------------------------------------------------


def test_gaps_flag_a_stalled_task():
    stalled = _replace_idle(_task("Nâng cấp uploader", page_id="p1"), days=6)
    gaps = describe_gaps([stalled], NOW, {})
    assert any("7 days" in gap and "Nâng cấp uploader" in gap for gap in gaps)


def test_gaps_flag_a_task_being_dodged():
    task = _replace_idle(_task("Dodged", page_id="p1"), days=0)
    gaps = describe_gaps([task], NOW, {"p1": 5})
    assert any("5 times" in gap for gap in gaps)


def test_gaps_count_missing_priority_and_estimate():
    tasks = [
        _task("a", page_id="a", priority="High", estimated="1 hour"),
        _task("b", page_id="b"),
        _task("c", page_id="c"),
    ]
    gaps = describe_gaps(tasks, NOW, {})
    assert any("2 tasks have no Priority" in gap for gap in gaps)
    assert any("2 tasks have no Estimated Time" in gap for gap in gaps)


def test_a_healthy_board_reports_no_gaps():
    tasks = [
        dataclasses.replace(
            _task("a", page_id="a", priority="High", estimated="1 hour"),
            created_at=NOW,
            edited_at=NOW,
        )
    ]
    assert describe_gaps(tasks, NOW, {"a": 1}) == []


def test_a_single_task_is_not_nagged_about_priority():
    """With one task there is nothing to order, so Priority is not yet needed."""
    only = dataclasses.replace(
        _task("a", page_id="a", estimated="1 hour"), created_at=NOW, edited_at=NOW
    )
    assert describe_gaps([only], NOW, {}) == []


def test_our_own_reminder_stamp_is_not_mistaken_for_progress():
    """Stamping Last Reminded bumps last_edited_time.

    Read naively that makes every reminded task look freshly worked on, so
    the reminder would erase the staleness it exists to report.
    """
    stamped = dataclasses.replace(
        _task("a", page_id="a"),
        created_at=NOW - timedelta(days=30),
        edited_at=NOW,
        last_reminded=NOW,
    )
    assert stamped.user_edited_at() is None
    assert stamped.idle_days(NOW) is None
    # Age still works: Notion never rewrites created_time.
    assert stamped.age_days(NOW) == 30
    assert any("30 days" in gap for gap in describe_gaps([stamped], NOW, {}))


def test_a_genuine_user_edit_is_still_seen():
    edited = dataclasses.replace(
        _task("a", page_id="a"),
        created_at=NOW - timedelta(days=30),
        edited_at=NOW - timedelta(days=4),
        last_reminded=NOW,
    )
    assert edited.user_edited_at() is not None
    assert edited.idle_days(NOW) == 4


def test_in_progress_stale_task_is_not_called_unstarted():
    """Work in flight that paused is a different problem from never starting."""
    task = _replace_idle(
        _task("a", page_id="a", status="In Progress"), days=9
    )
    gaps = describe_gaps([task], NOW, {})
    assert not any("Not Started" in gap for gap in gaps)


# --- task numbering -------------------------------------------------------


def test_task_number_survives_the_message_scrolling_away(settings):
    store = StateStore(settings.state_db_path)
    tasks = [_task("first", page_id="p-1"), _task("second", page_id="p-2")]
    notion_tasks.remember_task_order(store, tasks)
    assert notion_tasks.resolve_task_number(store, 2) == "p-2"


def test_out_of_range_task_number_resolves_to_nothing(settings):
    store = StateStore(settings.state_db_path)
    notion_tasks.remember_task_order(store, [_task(page_id="p-1")])
    assert notion_tasks.resolve_task_number(store, 5) == ""
    assert notion_tasks.resolve_task_number(store, 0) == ""


def test_missing_index_resolves_to_nothing(settings):
    store = StateStore(settings.state_db_path)
    assert notion_tasks.resolve_task_number(store, 1) == ""


def test_write_guard_blocks_edits_when_disabled(settings, monkeypatch):
    monkeypatch.setenv("NOTION_WRITE_ENABLED", "false")
    from daily_intel_bot.notion_client import NotionError

    with pytest.raises(NotionError, match="refusing to edit"):
        notion_tasks.complete_task(Settings(), NotionSchema(title="Task"), "p-1")


def test_board_load_degrades_instead_of_raising(settings, monkeypatch):
    """A Notion outage must cost the section, not the whole brief."""
    from daily_intel_bot.notion_client import NotionError

    def _boom(*_args, **_kwargs):
        raise NotionError("503 from Notion")

    monkeypatch.setattr(notion_tasks, "load_board", _boom)
    store = StateStore(settings.state_db_path)
    assert notion_tasks.load_board_quietly(settings, store) is None


def test_disabled_notion_is_skipped_without_network(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("NOTION_ENABLED", "false")
    settings = Settings()
    assert not notion_tasks.is_configured(settings)
    assert notion_tasks.load_board_quietly(settings, StateStore(settings.state_db_path)) is None


# --- dated notes ----------------------------------------------------------


def _note(title="Cancel sub", days=None, attendees=(), notes="", page_id="n1"):
    return NoteEntry(
        page_id=page_id,
        title=title,
        date=None if days is None else NOW + timedelta(days=days),
        attendees=tuple(attendees),
        notes=notes,
        url="https://notion.so/n1",
    )


def test_detects_the_live_notes_board():
    schema = detect_notes_schema(
        {
            "Created by": {"type": "created_by"},
            "Notes": {"type": "rich_text"},
            "Date": {"type": "date"},
            "Attendees": {"type": "people"},
            "Name": {"type": "title"},
        }
    )
    assert schema.title == "Name"
    assert schema.date == "Date"
    assert schema.attendees == "Attendees"
    assert schema.notes == "Notes"


def test_upcoming_notes_window_excludes_the_past_and_the_far_future():
    entries = [
        _note("yesterday", days=-1, page_id="a"),
        _note("today", days=0, page_id="b"),
        _note("next week", days=6, page_id="c"),
        _note("next year", days=300, page_id="d"),
        _note("undated", days=None, page_id="e"),
    ]
    titles = [e.title for e in upcoming_notes(entries, NOW, 14)]
    assert titles == ["today", "next week"]


def test_alerts_only_fire_as_the_date_nears():
    """A date six days out does not need a ping; it needs a calendar."""
    far = _note(days=6)
    near = _note(days=2, page_id="n2")
    assert notes_needing_alert([far], NOW, 3) == []
    assert notes_needing_alert([near], NOW, 3) == [near]


def test_note_voice_matches_how_close_the_date_is():
    persona = PERSONA_PROFILES["milf_teacher"]
    today = render_note_alert([_note(days=0)], NOW, persona)
    tomorrow = render_note_alert([_note(days=1)], NOW, persona)
    later = render_note_alert([_note(days=3)], NOW, persona)
    assert persona.note_today_line in today
    assert persona.note_soon_line in tomorrow
    assert persona.note_ahead_line in later


def test_note_line_says_when_in_plain_words():
    assert "today" in render_note_line(_note(days=0), NOW)
    assert "tomorrow" in render_note_line(_note(days=1), NOW)
    assert "in 5 days" in render_note_line(_note(days=5), NOW)


def test_note_line_lists_attendees_when_present():
    rendered = render_note_line(_note(days=1, attendees=("Tuấn", "Thạch")), NOW)
    assert "with Tuấn, Thạch" in rendered


def test_note_content_is_escaped():
    rendered = render_note_line(_note(title="A & B <b>", days=1), NOW)
    assert "A &amp; B &lt;b&gt;" in rendered


# --- AI nudge voice -------------------------------------------------------


def test_parse_nudge_voice_keeps_only_asides_in_range():
    voice = parse_nudge_voice(
        {
            "opening": "Seventy-four days, my lord.",
            "asides": [
                {"id": 0, "line": "This one rots."},
                {"id": 9, "line": "out of range"},
                {"id": 1, "line": "   "},
                "not a dict",
            ],
            "closing": "Do not falter.",
        },
        count=2,
    )
    assert voice.opening == "Seventy-four days, my lord."
    assert voice.asides == {0: "This one rots."}
    assert voice.closing == "Do not falter."


def test_parse_nudge_voice_rejects_an_empty_opening():
    with pytest.raises(ValueError):
        parse_nudge_voice({"opening": "  ", "asides": [], "closing": ""}, count=1)


def test_ai_voice_replaces_the_canned_line():
    persona = PERSONA_PROFILES["rot_maiden"]
    voice = NudgeVoice(
        opening="Seventy-four days, my lord.",
        asides={0: "This scroll gathers dust."},
        closing="Do not falter.",
    )
    rendered = render_reminder_batch([_task(page_id="p1")], NOW, persona, {}, voice)
    assert "Seventy-four days, my lord." in rendered
    assert "This scroll gathers dust." in rendered
    assert "Do not falter." in rendered
    assert persona.reminder_line not in rendered


def test_canned_line_is_used_when_the_model_is_unavailable():
    """A rate-limited model costs the flourish, not the reminder."""
    persona = PERSONA_PROFILES["rot_maiden"]
    rendered = render_reminder_batch([_task(page_id="p1")], NOW, persona, {}, None)
    assert persona.reminder_line in rendered


def test_ai_voice_is_escaped_like_any_other_text():
    voice = NudgeVoice(opening="A & B <b>", asides={}, closing="")
    rendered = render_reminder_batch([_task(page_id="p1")], NOW, None, {}, voice)
    assert "A &amp; B &lt;b&gt;" in rendered


def test_task_context_carries_the_numbers_the_voice_needs():
    task = _replace_idle(_task("Old one", page_id="p1"), days=10)
    context = task_context([task], NOW, {"p1": 4}, PERSONA_PROFILES["rot_maiden"], True)
    item = context["items"][0]
    assert item["days_open"] == 11
    assert item["times_nudged"] == 4
    assert context["persona"]["name"] == "The Rot Maiden"


def test_note_context_marks_itself_as_a_heads_up_not_a_scolding():
    context = note_context([_note(days=2)], NOW, None, True)
    assert context["kind"] == "dated_notes"
    assert context["items"][0]["days_until"] == 2


def test_nudge_voice_is_only_re_bought_when_the_batch_changes():
    """Hourly nudges over an unchanged board must not burn an AI call each time."""
    from daily_intel_bot.poller import _batch_fingerprint

    a, b = _task(page_id="p1"), _task(page_id="p2")
    same = _batch_fingerprint([a, b], {"p1": 1, "p2": 1})
    assert _batch_fingerprint([b, a], {"p1": 1, "p2": 1}) == same  # order-free
    assert _batch_fingerprint([a], {"p1": 1}) != same              # task left
    # Escalating past the nag threshold changes the tone, so buy a new voice.
    assert _batch_fingerprint([a, b], {"p1": NAG_THRESHOLD, "p2": 1}) != same
    # A bump that does not change the tone reuses what we already paid for.
    assert _batch_fingerprint([a, b], {"p1": 2, "p2": 1}) != same


def test_notes_are_flagged_the_day_before_and_on_the_day():
    tomorrow, today_ = _note(days=1, page_id="a"), _note(days=0, page_id="b")
    later = _note(days=2, page_id="c")
    flagged = notes_needing_alert([tomorrow, today_, later], NOW, 1)
    assert {e.page_id for e in flagged} == {"a", "b"}


def test_reminder_check_runs_once_per_clock_hour(settings, monkeypatch):
    """Nudges must land at 20:00 and 21:00, not drift to :35 forever."""
    import daily_intel_bot.poller as poller_module

    store = StateStore(settings.state_db_path)
    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    clock = {"now": datetime(2026, 9, 21, 20, 0, 3, tzinfo=tz)}
    monkeypatch.setattr(
        poller_module, "datetime", _FrozenClock(clock)
    )

    assert poller_module._hour_slot_turned(settings, store) is True
    # Later in the same hour: already handled.
    clock["now"] = datetime(2026, 9, 21, 20, 47, tzinfo=tz)
    assert poller_module._hour_slot_turned(settings, store) is False
    # The hour turns over.
    clock["now"] = datetime(2026, 9, 21, 21, 0, 2, tzinfo=tz)
    assert poller_module._hour_slot_turned(settings, store) is True


def test_a_restart_mid_hour_does_not_fire_a_second_nudge(settings, monkeypatch):
    import daily_intel_bot.poller as poller_module

    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    clock = {"now": datetime(2026, 9, 21, 21, 0, 1, tzinfo=tz)}
    monkeypatch.setattr(poller_module, "datetime", _FrozenClock(clock))

    store = StateStore(settings.state_db_path)
    assert poller_module._hour_slot_turned(settings, store) is True
    # Container restarts at 21:34; a fresh store reads the persisted slot.
    clock["now"] = datetime(2026, 9, 21, 21, 34, tzinfo=tz)
    restarted = StateStore(settings.state_db_path)
    assert poller_module._hour_slot_turned(settings, restarted) is False


class _FrozenClock:
    """Stands in for ``datetime`` so only ``now()`` is controlled."""

    def __init__(self, holder):
        self._holder = holder

    def now(self, tz=None):
        return self._holder["now"]


def test_a_deploy_mid_hour_waits_for_the_next_hour(settings, monkeypatch):
    """Restarting at 21:52 must not fire an off-hour nudge."""
    import daily_intel_bot.poller as poller_module

    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    clock = {"now": datetime(2026, 9, 21, 21, 52, tzinfo=tz)}
    monkeypatch.setattr(poller_module, "datetime", _FrozenClock(clock))
    monkeypatch.setattr(
        poller_module, "fetch_updates", lambda *a, **k: ([], 0)
    )
    fired = []
    monkeypatch.setattr(
        poller_module, "send_due_reminders", lambda *a: fired.append("nudge")
    )
    monkeypatch.setattr(poller_module, "send_due_note_alerts", lambda *a: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")

    poller_module.run_command_loop(Settings(), max_cycles=1)
    assert fired == []
