"""The command loop: read replies, act on them, answer.

Runs as its own long-lived process (``daily-intel-bot --serve``) next to the
daily scheduler. It is deliberately crash-tolerant — a bad update, a dropped
connection, or a failing handler must not end the loop, because a dead poller
silently turns every command the user sends into a no-op.
"""

from __future__ import annotations

import time

from daily_intel_bot.commands import handle_command
from daily_intel_bot.config import Settings
from daily_intel_bot.delivery import send_daily_digest
from daily_intel_bot.obs import get_logger
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
        except Exception as exc:  # noqa: BLE001 - the loop must outlive failures
            _LOG.exception("command loop error: %s", type(exc).__name__)
            time.sleep(ERROR_SLEEP_SECONDS)


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
