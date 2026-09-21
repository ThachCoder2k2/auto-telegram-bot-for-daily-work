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
        voice = persona.nag_line if nagging else persona.reminder_line
        lines.append(f"{persona.icon} <b>{escape(persona.name)}</b>")
        lines.append(f"<blockquote>{escape(voice)}</blockquote>")
    else:
        lines.append("⏰ <b>Nhắc việc</b>" if not nagging else "⏰ <b>Vẫn chưa xong</b>")
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

    gaps = describe_gaps(tasks, now, reminder_counts)
    if gaps:
        lines.append("")
        lines.extend(f"⚠️ <i>{escape(gap)}</i>" for gap in gaps)

    lines.append("")
    lines.append("<code>/ndone &lt;số&gt;</code> khi xong · <code>/ntasks</code> xem tất cả")
    return "\n".join(lines)


def _task_signals(task: NotionTask, now: datetime, count: int) -> list[str]:
    """The per-task facts worth the line: effort, idleness, nudge count."""
    parts: list[str] = []
    if task.estimated_time:
        parts.append(task.estimated_time)
    else:
        parts.append("chưa ước lượng")

    idle = task.idle_days(now)
    if idle is not None:
        parts.append("chạm hôm nay" if idle == 0 else f"nằm im {idle} ngày")
    if count > 1:
        parts.append(f"nhắc lần {count}")
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

    stalled = [
        task
        for task in tasks
        if (task.idle_days(now) or 0) >= STALE_DAYS
        and task.status.strip().lower() == "not started"
    ]
    if stalled:
        worst = max(stalled, key=lambda t: t.idle_days(now) or 0)
        gaps.append(
            f"Chưa bắt đầu {worst.idle_days(now)} ngày: {_short(worst.title)}"
            + (f" (+{len(stalled) - 1} việc nữa)" if len(stalled) > 1 else "")
        )

    dodged = [
        task for task in tasks if reminder_counts.get(task.page_id, 0) >= NAG_THRESHOLD
    ]
    if dodged:
        worst = max(dodged, key=lambda t: reminder_counts.get(t.page_id, 0))
        count = reminder_counts.get(worst.page_id, 0)
        gaps.append(
            f"Nhắc {count} lần vẫn chưa động: {_short(worst.title)} "
            "— chia nhỏ ra, hoặc /ndone nếu không còn cần"
        )

    missing_priority = [task for task in tasks if not task.priority.strip()]
    if missing_priority:
        gaps.append(
            f"{len(missing_priority)} việc chưa đặt Priority nên không biết làm cái nào trước"
        )

    missing_estimate = [task for task in tasks if not task.estimated_time.strip()]
    if missing_estimate:
        gaps.append(
            f"{len(missing_estimate)} việc chưa có Estimated Time nên không biết có nhét vừa hôm nay không"
        )
    return gaps


def _short(value: str, limit: int = 42) -> str:
    value = value.strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"
