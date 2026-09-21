"""Decide which Notion tasks are due for a nudge.

The board already carried ``Reminder``, ``Reminder Frequency`` and
``Last Reminded`` columns, but nothing ever read them — they only mean
something once a process compares them against the clock. This module is that
comparison, kept free of I/O so the scheduling rules can be tested directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape

from daily_intel_bot.notion_client import NoteEntry, NotionTask
from daily_intel_bot.nudge_voice import NudgeVoice
from daily_intel_bot.persona import PersonaProfile


# Notion's own wording, mapped to intervals. Unknown values are ignored rather
# than guessed at, so a new option cannot silently produce hourly spam.
FREQUENCY_INTERVALS: dict[str, timedelta] = {
    "every hour": timedelta(hours=1),
    "every 2 hours": timedelta(hours=2),
    "every 4 hours": timedelta(hours=4),
    "every day": timedelta(days=1),
    "every week": timedelta(days=7),
}
# "Once" fires a single time and never repeats.
ONCE = "once"
NEVER = "none"

PRIORITY_ICONS = {"high": "🔴", "medium": "🟡", "low": "⚪"}
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True, slots=True)
class ReminderWindow:
    """Hours of the day during which nudges may be sent."""

    start_hour: int
    end_hour: int

    def is_open(self, now: datetime) -> bool:
        """True inside waking hours.

        A window that wraps midnight (22 -> 6) is treated as the union of both
        ends of the day rather than an empty range.
        """
        if self.start_hour == self.end_hour:
            return True  # degenerate config means "always"
        if self.start_hour < self.end_hour:
            return self.start_hour <= now.hour < self.end_hour
        return now.hour >= self.start_hour or now.hour < self.end_hour


def due_reminders(
    tasks: list[NotionTask],
    now: datetime,
    window: ReminderWindow,
    tolerance: timedelta = timedelta(0),
    default_frequency: str = "",
) -> list[NotionTask]:
    """Tasks whose reminder interval has elapsed, most urgent first.

    Outside the window nothing is due; a reminder that came up at 03:00 simply
    fires at the first check after the window reopens.

    ``tolerance`` lets a check that lands fractionally early still fire. When
    the check cadence equals the reminder interval — hourly checks against an
    "Every hour" task — each check measures a hair under the interval and
    would otherwise skip, silently halving the reminder rate.

    ``default_frequency`` covers tasks with Reminder ticked but no frequency
    chosen, which otherwise look enabled while doing nothing.
    """
    if not window.is_open(now):
        return []
    due = [
        task for task in tasks if _is_due(task, now, tolerance, default_frequency)
    ]
    return sorted(due, key=_urgency)


def _is_due(
    task: NotionTask,
    now: datetime,
    tolerance: timedelta = timedelta(0),
    default_frequency: str = "",
) -> bool:
    if not task.reminder or task.is_done:
        return False
    frequency = task.reminder_frequency.strip().lower()
    if not frequency:
        # Ticking Reminder and leaving Frequency blank reads as "remind me",
        # so fall back rather than silently doing nothing. An explicit "None"
        # still means never.
        frequency = default_frequency.strip().lower()
    if not frequency or frequency == NEVER:
        return False
    if frequency == ONCE:
        return task.last_reminded is None
    interval = FREQUENCY_INTERVALS.get(frequency)
    if interval is None:
        return False
    if task.last_reminded is None:
        return True
    return now - task.last_reminded >= interval - tolerance


def _urgency(task: NotionTask) -> tuple[int, float]:
    priority = PRIORITY_ORDER.get(task.priority.strip().lower(), 3)
    # Longest-overdue first within a priority band.
    age = -(task.last_reminded.timestamp() if task.last_reminded else 0.0)
    return (priority, age)


def sort_tasks(tasks: list[NotionTask]) -> list[NotionTask]:
    """Order the board for display: priority first, then quickest wins.

    This board has no due-date column, so 'what is most urgent' has to come
    from Priority, and ties are broken by estimated effort — a 15 minute task
    is easier to actually start than an all-day one.
    """
    return sorted(
        tasks,
        key=lambda task: (
            PRIORITY_ORDER.get(task.priority.strip().lower(), 3),
            _effort_minutes(task.estimated_time),
            task.title.lower(),
        ),
    )


_EFFORT_MINUTES = {
    "5 min": 5,
    "15 min": 15,
    "30 min": 30,
    "1 hour": 60,
    "2 hours": 120,
    "4+ hours": 240,
    "all day": 480,
}


def _effort_minutes(label: str) -> int:
    # Unsized tasks sort after sized ones: an unknown is not a quick win.
    return _EFFORT_MINUTES.get(label.strip().lower(), 9999)


def render_task_line(index: int, task: NotionTask) -> str:
    icon = PRIORITY_ICONS.get(task.priority.strip().lower(), "⚪")
    parts = [f"{index}. {icon} <b>{escape(task.title)}</b>"]
    meta = [value for value in (task.status, task.estimated_time) if value]
    if meta:
        parts.append(f" · <i>{escape(' · '.join(meta))}</i>")
    return "".join(parts)


# A task nudged this many times without being touched has stopped being
# forgotten and started being avoided.
NAG_THRESHOLD = 3
# Days untouched before a task counts as stalling.
STALE_DAYS = 3


def render_reminder_batch(
    tasks: list[NotionTask],
    now: datetime,
    persona: PersonaProfile | None,
    reminder_counts: dict[str, int],
    voice: NudgeVoice | None = None,
) -> str:
    """One message for the whole batch, in the day's persona voice.

    Sending a separate identically-worded message per task turned three
    reminders into three walls of boilerplate. This keeps the framing once and
    spends the space on what is actually different between the tasks: how long
    each has sat, how often it has been nudged, and what is missing from it.
    """
    worst_count = max((reminder_counts.get(t.page_id, 0) for t in tasks), default=0)
    nagging = worst_count >= NAG_THRESHOLD

    lines: list[str] = []
    if persona:
        # Prefer what the model wrote against today's actual board; the canned
        # lines are the offline fallback, not the default.
        opening = voice.opening if voice else (
            persona.nag_line if nagging else persona.reminder_line
        )
        lines.append(f"{persona.icon} <b>{escape(persona.name)}</b>")
        lines.append(f"<blockquote>{escape(opening)}</blockquote>")
    elif voice:
        lines.append(f"<blockquote>{escape(voice.opening)}</blockquote>")
    else:
        lines.append("⏰ <b>Still open</b>" if not nagging else "⏰ <b>Still open. Again.</b>")
    lines.append("")

    for index, task in enumerate(tasks, start=1):
        icon = PRIORITY_ICONS.get(task.priority.strip().lower(), "⚪")
        title = escape(task.title)
        if task.url:
            title = f'<a href="{escape(task.url)}">{title}</a>'
        lines.append(f"{icon} <b>{index}. {title}</b>")
        detail = _task_signals(task, now, reminder_counts.get(task.page_id, 0))
        if detail:
            lines.append(f"   <i>{escape(' · '.join(detail))}</i>")
        aside = voice.asides.get(index - 1) if voice else None
        if aside:
            lines.append(f"   <i>{escape(aside)}</i>")

    gaps = describe_gaps(tasks, now, reminder_counts)
    if gaps:
        lines.append("")
        lines.extend(f"⚠️ <i>{escape(gap)}</i>" for gap in gaps)

    if voice and voice.closing:
        lines.append("")
        lines.append(f"<i>{escape(voice.closing)}</i>")

    lines.append("")
    lines.append(
        "<code>/ndone &lt;n&gt;</code> when it's done · "
        "<code>/ntasks</code> for the full board"
    )
    return "\n".join(lines)


def _task_signals(task: NotionTask, now: datetime, count: int) -> list[str]:
    """Only what is specific to this task and not obvious from the title.

    An earlier version printed a field for every task whether or not it said
    anything, so three tasks rendered three identical grey lines. A detail
    line now appears only when it carries news; otherwise the task is just a
    title, which is enough.
    """
    parts: list[str] = []
    if task.estimated_time:
        parts.append(task.estimated_time)

    # Age comes from created_time, which Notion never rewrites, so it survives
    # our own Last Reminded stamps. Only worth saying once it is old.
    age = task.age_days(now)
    if age is not None and age >= STALE_DAYS:
        parts.append(f"open {age}d")

    idle = task.idle_days(now)
    if idle is not None and idle >= STALE_DAYS:
        parts.append(f"last touched {idle}d ago")

    if count > 1:
        parts.append(f"nudge #{count}")
    if task.status and task.status.strip().lower() != "not started":
        parts.append(task.status)
    return parts


def describe_gaps(
    tasks: list[NotionTask],
    now: datetime,
    reminder_counts: dict[str, int],
) -> list[str]:
    """Name what is missing, so a nudge says more than the task's own title.

    "What am I not seeing" is the question a reminder should answer: which
    task is rotting, which is being dodged, and which cannot be prioritised
    because it was never given a priority or a size.
    """
    gaps: list[str] = []

    # Age, not last-edit: our own reminder stamps keep bumping last-edit, so
    # measuring staleness that way would report every task as fresh.
    stalled = [
        task
        for task in tasks
        if (task.age_days(now) or 0) >= STALE_DAYS
        and task.status.strip().lower() == "not started"
    ]
    if stalled:
        worst = max(stalled, key=lambda t: t.age_days(now) or 0)
        gaps.append(
            f"Open {worst.age_days(now)} days, still Not Started: {_short(worst.title)}"
            + (f" (+{len(stalled) - 1} more)" if len(stalled) > 1 else "")
        )

    dodged = [
        task for task in tasks if reminder_counts.get(task.page_id, 0) >= NAG_THRESHOLD
    ]
    if dodged:
        worst = max(dodged, key=lambda t: reminder_counts.get(t.page_id, 0))
        count = reminder_counts.get(worst.page_id, 0)
        gaps.append(
            f"Nudged {count} times, still untouched: {_short(worst.title)} "
            "— split it smaller, or /ndone it if it no longer matters"
        )

    missing_priority = [task for task in tasks if not task.priority.strip()]
    # With a single task there is nothing to order, so a missing Priority is
    # not yet a problem worth a line.
    if missing_priority and len(tasks) > 1:
        scope = "All " if len(missing_priority) == len(tasks) else ""
        gaps.append(
            f"{scope}{len(missing_priority)} tasks have no Priority "
            "→ nothing tells you which to pick first"
        )

    missing_estimate = [task for task in tasks if not task.estimated_time.strip()]
    if missing_estimate:
        scope = "All " if len(missing_estimate) == len(tasks) else ""
        gaps.append(
            f"{scope}{len(missing_estimate)} tasks have no Estimated Time "
            "→ you can't tell what fits in today"
        )
    return gaps


# Dated entries are not tasks: nothing rots by being left alone, the date
# simply arrives. So they surface rarely, and only as the day approaches.
NOTE_ALERT_DAYS = 3


def upcoming_notes(
    entries: list[NoteEntry],
    now: datetime,
    lookahead_days: int,
) -> list[NoteEntry]:
    """Dated entries from today up to ``lookahead_days`` out, soonest first."""
    upcoming = [
        entry
        for entry in entries
        if entry.days_until(now) is not None
        and 0 <= (entry.days_until(now) or 0) <= lookahead_days
    ]
    return sorted(upcoming, key=lambda entry: entry.days_until(now) or 0)


def notes_needing_alert(
    entries: list[NoteEntry],
    now: datetime,
    alert_days: int = NOTE_ALERT_DAYS,
) -> list[NoteEntry]:
    """Entries close enough that a separate ping is warranted."""
    return [
        entry
        for entry in upcoming_notes(entries, now, alert_days)
        if (entry.days_until(now) or 0) <= alert_days
    ]


def describe_when(days: int) -> str:
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"in {days} days"


def render_note_line(entry: NoteEntry, now: datetime) -> str:
    days = entry.days_until(now)
    title = escape(entry.title)
    if entry.url:
        title = f'<a href="{escape(entry.url)}">{title}</a>'
    icon = "🔴" if days is not None and days <= 1 else "📅"
    line = f"{icon} <b>{title}</b>"
    bits: list[str] = []
    if entry.date is not None:
        bits.append(entry.date.strftime("%a %d %b"))
    if days is not None:
        bits.append(describe_when(days))
    if entry.attendees:
        bits.append("with " + ", ".join(entry.attendees))
    if bits:
        line += f"\n   <i>{escape(' · '.join(bits))}</i>"
    if entry.notes:
        line += f"\n   <i>{escape(_short(entry.notes, 90))}</i>"
    return line


def render_note_alert(
    entries: list[NoteEntry],
    now: datetime,
    persona: PersonaProfile | None,
    voice: NudgeVoice | None = None,
) -> str:
    """A rare, dated heads-up — distinct in tone from the task nagging."""
    # Explicit None check: "days or 99" would read an entry due *today* as 99
    # days out, because zero is falsy.
    horizons = [
        entry.days_until(now) for entry in entries if entry.days_until(now) is not None
    ]
    soonest = min(horizons) if horizons else 99
    lines: list[str] = []
    if persona:
        opening = voice.opening if voice else _note_voice(persona, soonest)
        lines.append(f"{persona.icon} <b>{escape(persona.name)}</b>")
        lines.append(f"<blockquote>{escape(opening)}</blockquote>")
    elif voice:
        lines.append(f"<blockquote>{escape(voice.opening)}</blockquote>")
    else:
        lines.append("🗓️ <b>Coming up</b>")
    lines.append("")
    for index, entry in enumerate(entries):
        lines.append(render_note_line(entry, now))
        aside = voice.asides.get(index) if voice else None
        if aside:
            lines.append(f"   <i>{escape(aside)}</i>")
    if voice and voice.closing:
        lines.append("")
        lines.append(f"<i>{escape(voice.closing)}</i>")
    return "\n".join(lines)


def _note_voice(persona: PersonaProfile, days: int) -> str:
    """Date-aware framing, so an entry due today does not sound like one a week out."""
    if days <= 0:
        return persona.note_today_line
    if days == 1:
        return persona.note_soon_line
    return persona.note_ahead_line


def _short(value: str, limit: int = 42) -> str:
    value = value.strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"
