"""What happens when a button is tapped.

Three days of nudges produced 160 messages and zero replies. Every reply path
needed the user to recall a command, find a number and type it; a tap needs
none of that. These handlers are the other end of that tap.

Actions are deliberately reversible in Notion — Done flips a status, Stop
unticks a checkbox, Snooze touches nothing there at all — so a mis-tap is
never destructive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from daily_intel_bot import notion_tasks
from daily_intel_bot.config import Settings
from daily_intel_bot.notion_client import NotionError
from daily_intel_bot.obs import get_logger
from daily_intel_bot.state_store import StateStore


_LOG = get_logger("callbacks")

ACTION_DONE = "done"
ACTION_SNOOZE = "snz"
ACTION_STOP = "stop"
ACTION_SPLIT = "split"
ACTION_LATER = "later"
# Days a "not now" answer at an escalation point buys.
LATER_DAYS = 7
# Telegram caps callback_data at 64 bytes; "snz:" plus a 36-char Notion uuid
# leaves room to spare, but the prefixes stay short on purpose.
CALLBACK_SEPARATOR = ":"


@dataclass(frozen=True, slots=True)
class CallbackResult:
    """What to tell the user, and how to amend the message they tapped."""

    toast: str
    note: str = ""
    handled_page_id: str = ""


def parse_action(data: str) -> tuple[str, str]:
    action, _, page_id = data.partition(CALLBACK_SEPARATOR)
    return action.strip(), page_id.strip()


def button_row(page_id: str, snooze_hours: int) -> list[dict[str, str]]:
    """The three replies a nudge should accept without typing."""
    return [
        {"text": "✅ Done", "callback_data": f"{ACTION_DONE}{CALLBACK_SEPARATOR}{page_id}"},
        {
            "text": f"😴 {snooze_hours}h",
            "callback_data": f"{ACTION_SNOOZE}{CALLBACK_SEPARATOR}{page_id}",
        },
        {"text": "🔕 Stop", "callback_data": f"{ACTION_STOP}{CALLBACK_SEPARATOR}{page_id}"},
    ]


def escalation_row(page_id: str) -> list[dict[str, str]]:
    """The replies that actually move a stuck task, rather than repeat it."""
    return [
        {
            "text": "✂️ Too big",
            "callback_data": f"{ACTION_SPLIT}{CALLBACK_SEPARATOR}{page_id}",
        },
        {
            "text": f"🗓 {LATER_DAYS}d",
            "callback_data": f"{ACTION_LATER}{CALLBACK_SEPARATOR}{page_id}",
        },
        {"text": "🔕 Drop it", "callback_data": f"{ACTION_STOP}{CALLBACK_SEPARATOR}{page_id}"},
    ]


def handle(settings: Settings, data: str) -> CallbackResult:
    """Apply a tapped button. Never raises: a failed tap must still answer."""
    action, page_id = parse_action(data)
    if not page_id:
        return CallbackResult(toast="Unrecognised button")

    store = StateStore(settings.state_db_path)
    now = datetime.now(ZoneInfo(settings.timezone))
    try:
        if action == ACTION_DONE:
            board = notion_tasks.load_board(settings, store)
            notion_tasks.complete_task(settings, board.schema, page_id)
            store.clear_reminder_log(page_id)
            title = _title(board, page_id)
            return CallbackResult(
                toast="Marked done in Notion",
                note=f"✅ <b>{escape(title)}</b> — done",
                handled_page_id=page_id,
            )

        if action == ACTION_SNOOZE:
            until = now + timedelta(hours=settings.notion_snooze_hours)
            store.snooze_task(page_id, until)
            return CallbackResult(
                toast=f"Quiet until {until.strftime('%H:%M')}",
                note=f"😴 Snoozed until {until.strftime('%H:%M')}",
                handled_page_id=page_id,
            )

        if action == ACTION_LATER:
            until = now + timedelta(days=LATER_DAYS)
            store.snooze_task(page_id, until)
            return CallbackResult(
                toast=f"Back on {until.strftime('%d %b')}",
                note=f"🗓 Parked until {until.strftime('%d %b')}",
                handled_page_id=page_id,
            )

        if action == ACTION_SPLIT:
            # The bot cannot split the task for the user, but it can stop
            # nagging about the version that is too big to start.
            until = now + timedelta(days=1)
            store.snooze_task(page_id, until)
            return CallbackResult(
                toast="Noted — split it in Notion",
                note=(
                    "✂️ Marked as too big. Split it into a first step in "
                    "Notion; quiet until tomorrow."
                ),
                handled_page_id=page_id,
            )

        if action == ACTION_STOP:
            board = notion_tasks.load_board(settings, store)
            notion_tasks.stop_reminding(settings, board.schema, page_id)
            store.clear_reminder_log(page_id)
            title = _title(board, page_id)
            return CallbackResult(
                toast="Reminder switched off in Notion",
                note=f"🔕 <b>{escape(title)}</b> — reminders off",
                handled_page_id=page_id,
            )
    except NotionError as exc:
        _LOG.warning("callback %s failed: %s", action, exc)
        return CallbackResult(toast=f"Notion: {exc}"[:190])
    except Exception as exc:  # noqa: BLE001 - a tap must always get an answer
        _LOG.exception("callback %s crashed", action)
        return CallbackResult(toast=f"Failed: {type(exc).__name__}")

    return CallbackResult(toast="Unrecognised button")


def _title(board, page_id: str) -> str:
    for task in board.tasks:
        if task.page_id == page_id:
            return task.title
    return "task"


def strip_handled_row(keyboard: list, page_id: str) -> list:
    """Drop the buttons for a task that has just been dealt with.

    Leaving them in place invites a second tap on something already handled.
    """
    remaining = []
    for row in keyboard:
        if not isinstance(row, list):
            continue
        if any(
            isinstance(button, dict)
            and parse_action(str(button.get("callback_data", "")))[1] == page_id
            for button in row
        ):
            continue
        remaining.append(row)
    return remaining
