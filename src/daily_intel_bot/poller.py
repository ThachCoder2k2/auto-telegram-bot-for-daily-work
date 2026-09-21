"""The command loop: read replies, act on them, answer.

Runs as its own long-lived process (``daily-intel-bot --serve``) next to the
daily scheduler. It is deliberately crash-tolerant — a bad update, a dropped
connection, or a failing handler must not end the loop, because a dead poller
silently turns every command the user sends into a no-op.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import time
from zoneinfo import ZoneInfo

from daily_intel_bot.commands import handle_command
from daily_intel_bot.config import Settings
from daily_intel_bot.delivery import send_daily_digest
from daily_intel_bot.notion_tasks import load_board_quietly, stamp_reminded
from daily_intel_bot.obs import get_logger
from daily_intel_bot.reminders import ReminderWindow, due_reminders, render_reminder
from daily_intel_bot.state_store import StateStore
from daily_intel_bot.telegram_client import send_message
from daily_intel_bot.telegram_updates import (
    OFFSET_META_KEY,
    fetch_updates,
)


_LOG = get_logger("poller")

# Backoff after an unexpected loop error, so a persistent failure does not
# become a hot loop against the Telegram API.
ERROR_SLEEP_SECONDS = 15


def run_command_loop(settings: Settings, max_cycles: int | None = None) -> None:
    """Poll for commands until interrupted.

    ``max_cycles`` exists for tests; production passes ``None`` for forever.
    """
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")

    store = StateStore(settings.state_db_path)
    offset = _load_offset(settings, store)
    _LOG.info("command loop up (offset=%d)", offset)

    cycles = 0
    last_reminder_check = 0.0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        try:
            commands, next_offset = fetch_updates(
                settings,
                offset,
                timeout=settings.command_poll_seconds,
            )
            for command in commands:
                _LOG.info("command from %s: %s", command.from_user, command.text)
                _dispatch(settings, command.text)
            if next_offset != offset:
                offset = next_offset
                store.set_meta(OFFSET_META_KEY, str(offset))

            # The poll already wakes every ~30s, so reminders ride along on
            # this loop instead of needing a second scheduler.
            if (
                time.monotonic() - last_reminder_check
                >= settings.notion_reminder_check_seconds
            ):
                last_reminder_check = time.monotonic()
                send_due_reminders(settings, store)
        except Exception as exc:  # noqa: BLE001 - the loop must outlive failures
            _LOG.exception("command loop error: %s", type(exc).__name__)
            time.sleep(ERROR_SLEEP_SECONDS)


def send_due_reminders(settings: Settings, store: StateStore) -> int:
    """Nudge every Notion task whose reminder interval has elapsed.

    Returns the number sent. Each nudge stamps ``Last Reminded`` so the
    interval actually advances; a task whose stamp fails is simply nudged
    again next cycle.
    """
    if not settings.notion_reminders_enabled:
        return 0
    board = load_board_quietly(settings, store)
    if board is None:
        return 0

    now = datetime.now(ZoneInfo(settings.timezone))
    window = ReminderWindow(
        start_hour=settings.notion_quiet_start,
        end_hour=settings.notion_quiet_end,
    )
    # Half the check cadence: enough slack that a check landing just shy of
    # the interval still fires, without letting a reminder arrive early enough
    # to matter.
    tolerance = timedelta(seconds=settings.notion_reminder_check_seconds / 2)
    due = due_reminders(board.tasks, now, window, tolerance)
    sent = 0
    for task in due:
        try:
            send_message(settings, render_reminder(task))
        except Exception as exc:  # noqa: BLE001 - one bad nudge must not stop the rest
            _LOG.warning("reminder send failed for %s: %s", task.title, exc)
            continue
        stamp_reminded(settings, board.schema, task.page_id, now)
        sent += 1
    if sent:
        _LOG.info("sent %d Notion reminder(s)", sent)
    return sent


def _dispatch(settings: Settings, text: str) -> None:
    result = handle_command(settings, text)
    if result.reply:
        try:
            send_message(settings, result.reply)
        except Exception as exc:  # noqa: BLE001 - reply failure must not skip the action
            _LOG.warning("reply failed: %s: %s", type(exc).__name__, exc)

    if result.action != "send_digest":
        return
    try:
        delivered = send_daily_digest(settings)
        _LOG.info(
            "on-demand digest sent (%d message(s), %d items)",
            len(delivered.message_ids),
            delivered.items_sent,
        )
    except Exception as exc:  # noqa: BLE001 - report the failure to the user
        _LOG.exception("on-demand digest failed")
        try:
            send_message(
                settings,
                f"💥 Digest failed: {type(exc).__name__}: {exc}",
            )
        except Exception:  # noqa: BLE001 - nothing left to do if this fails too
            _LOG.warning("could not report digest failure")


def _load_offset(settings: Settings, store: StateStore) -> int:
    raw = store.get_meta(OFFSET_META_KEY, "")
    if raw:
        try:
            return int(raw)
        except ValueError:
            _LOG.warning("bad stored offset %r, priming from the latest update", raw)

    # No stored offset: this is a first start. Telegram keeps updates for 24h,
    # so starting from zero would replay yesterday's messages and could close
    # a task the user already handled. Skip straight to the newest update.
    offset = prime_offset(settings)
    store.set_meta(OFFSET_META_KEY, str(offset))
    _LOG.info("no stored offset; skipping backlog and starting at %d", offset)
    return offset


def prime_offset(settings: Settings) -> int:
    """Return the offset just past the newest pending update."""
    # offset=-1 asks Telegram for only the most recent update.
    _commands, offset = fetch_updates(settings, -1, timeout=0)
    return max(0, offset)
