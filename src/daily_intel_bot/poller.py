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
from daily_intel_bot.notion_tasks import (
    load_board_quietly,
    load_notes_quietly,
    stamp_reminded,
)
from daily_intel_bot.gemini_client import generate_nudge_voice_gemini
from daily_intel_bot.nudge_voice import (
    NudgeVoice,
    note_context,
    parse_nudge_voice,
    task_context,
)
from daily_intel_bot.obs import get_logger, is_transient_http_error, with_retries
from daily_intel_bot.openai_client import generate_nudge_voice
from daily_intel_bot.persona import select_persona
from daily_intel_bot.reminders import (
    ReminderWindow,
    due_reminders,
    notes_needing_alert,
    render_note_alert,
    render_reminder_batch,
)
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
                send_due_note_alerts(settings, store)
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
    due = due_reminders(
        board.tasks,
        now,
        window,
        tolerance,
        settings.notion_default_frequency,
    )
    if not due:
        return 0

    # Count the nudge before rendering so the message can say "lần 4" and
    # escalate its tone on the same pass.
    counts = {
        task.page_id: store.record_reminder(task.page_id, task.title, now)
        for task in due
    }
    persona = select_persona(
        enabled=settings.bot_persona_enabled,
        rotation=settings.bot_persona_rotation,
        pool=settings.bot_persona_pool,
        forced_key=settings.bot_persona_force,
        now=now,
    )
    voice = _write_voice(
        settings,
        task_context(due, now, counts, persona, settings.bot_persona_safe_mode),
        len(due),
    )
    try:
        # One batched message: three separately-worded nudges an hour apart is
        # a notification, three identical ones at once is just noise.
        send_message(
            settings, render_reminder_batch(due, now, persona, counts, voice)
        )
    except Exception as exc:  # noqa: BLE001 - a failed nudge retries next cycle
        _LOG.warning("reminder send failed: %s: %s", type(exc).__name__, exc)
        return 0

    for task in due:
        stamp_reminded(settings, board.schema, task.page_id, now)
    _LOG.info("sent 1 batched nudge covering %d Notion task(s)", len(due))
    return len(due)


def send_due_note_alerts(settings: Settings, store: StateStore) -> int:
    """Ping about dated entries as their date nears.

    Deliberately rare: a meeting or deadline does not decay by being ignored
    the way an open task does, so pinging hourly would be pure noise. At most
    one ping per entry per day, and only inside the alert window.
    """
    entries = load_notes_quietly(settings, store)
    if not entries:
        return 0

    now = datetime.now(ZoneInfo(settings.timezone))
    window = ReminderWindow(
        start_hour=settings.notion_quiet_start,
        end_hour=settings.notion_quiet_end,
    )
    if not window.is_open(now):
        return 0

    today = now.date().isoformat()
    fresh = [
        entry
        for entry in notes_needing_alert(entries, now, settings.notion_notes_alert_days)
        if store.get_meta(_note_alert_key(entry.page_id), "") != today
    ]
    if not fresh:
        return 0

    persona = select_persona(
        enabled=settings.bot_persona_enabled,
        rotation=settings.bot_persona_rotation,
        pool=settings.bot_persona_pool,
        forced_key=settings.bot_persona_force,
        now=now,
    )
    voice = _write_voice(
        settings,
        note_context(fresh, now, persona, settings.bot_persona_safe_mode),
        len(fresh),
    )
    try:
        send_message(settings, render_note_alert(fresh, now, persona, voice))
    except Exception as exc:  # noqa: BLE001 - retried on the next check
        _LOG.warning("note alert failed: %s: %s", type(exc).__name__, exc)
        return 0

    for entry in fresh:
        store.set_meta(_note_alert_key(entry.page_id), today)
    _LOG.info("sent note alert covering %d dated entry(ies)", len(fresh))
    return len(fresh)


def _write_voice(
    settings: Settings,
    context: dict[str, object],
    count: int,
) -> NudgeVoice | None:
    """Have the AI backend write the nudge, or fall back to canned lines.

    The brief reads as alive because a model writes it against that day's real
    material; nudges deserve the same treatment. Best-effort by design: a
    rate-limited model costs the flourish, not the reminder.
    """
    if not settings.notion_ai_voice_enabled:
        return None
    use_gemini = settings.ai_provider == "gemini"
    if use_gemini:
        if not settings.gemini_api_key:
            return None
    elif not settings.openai_enabled or not settings.openai_api_key:
        return None

    def _call() -> dict:
        if use_gemini:
            return generate_nudge_voice_gemini(
                settings.gemini_api_key, settings.gemini_model, context
            )
        return generate_nudge_voice(
            settings.openai_api_key, settings.openai_model, context
        )

    try:
        payload = with_retries(
            _call,
            attempts=2,
            backoff=2.0,
            logger=_LOG,
            label="nudge voice",
            should_retry=is_transient_http_error,
        )
        return parse_nudge_voice(payload, count)
    except Exception as exc:  # noqa: BLE001 - voice is a flourish, not the point
        _LOG.warning(
            "nudge voice unavailable, using canned lines: %s: %s",
            type(exc).__name__,
            exc,
        )
        return None


def _note_alert_key(page_id: str) -> str:
    return f"notion_note_alert:{page_id}"


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
