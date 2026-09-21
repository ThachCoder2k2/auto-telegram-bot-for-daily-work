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

from daily_intel_bot.notion_client import NotionTask


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
) -> list[NotionTask]:
    """Tasks whose reminder interval has elapsed, most urgent first.

    Outside the window nothing is due; a reminder that came up at 03:00 simply
    fires at the first check after the window reopens.

    ``tolerance`` lets a check that lands fractionally early still fire. When
    the check cadence equals the reminder interval — hourly checks against an
    "Every hour" task — each check measures a hair under the interval and
    would otherwise skip, silently halving the reminder rate.
    """
    if not window.is_open(now):
        return []
    due = [task for task in tasks if _is_due(task, now, tolerance)]
    return sorted(due, key=_urgency)


def _is_due(
    task: NotionTask,
    now: datetime,
    tolerance: timedelta = timedelta(0),
) -> bool:
    if not task.reminder or task.is_done:
        return False
    frequency = task.reminder_frequency.strip().lower()
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


def render_reminder(task: NotionTask) -> str:
    icon = PRIORITY_ICONS.get(task.priority.strip().lower(), "⚪")
    lines = [f"⏰ <b>Nhắc việc</b>", f"{icon} <b>{escape(task.title)}</b>"]
    meta = [
        value
        for value in (task.status, task.estimated_time, task.reminder_frequency)
        if value
    ]
    if meta:
        lines.append(f"<i>{escape(' · '.join(meta))}</i>")
    if task.url:
        lines.append(f'🔗 <a href="{escape(task.url)}">Mở trong Notion</a>')
    lines.append("<code>/ntasks</code> để xem danh sách · <code>/ndone n</code> khi xong")
    return "\n".join(lines)
