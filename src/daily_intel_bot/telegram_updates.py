"""Inbound side of the Telegram bot: long-poll getUpdates.

The digest has always told the user to "reply with done / blocked / change
task", but nothing ever read those replies. This module is the missing half:
it pulls updates with a long poll and hands the text to ``commands``.

Offset bookkeeping lives in ``StateStore.meta`` so a container restart resumes
where it stopped instead of replaying or dropping messages.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from urllib import error, parse, request

from daily_intel_bot.config import Settings
from daily_intel_bot.obs import get_logger


_LOG = get_logger("telegram_updates")

OFFSET_META_KEY = "telegram_update_offset"
# Telegram allows up to 50s; keep headroom under the socket timeout below.
LONG_POLL_SECONDS = 30


@dataclass(frozen=True, slots=True)
class TelegramCommand:
    update_id: int
    chat_id: str
    text: str
    from_user: str


def fetch_updates(
    settings: Settings,
    offset: int,
    timeout: int = LONG_POLL_SECONDS,
) -> tuple[list[TelegramCommand], int]:
    """Long-poll for new messages.

    Returns the parsed commands plus the next offset to use. On a network or
    API error the offset is returned unchanged so the caller simply retries.
    """
    if not settings.telegram_bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is missing")

    query = parse.urlencode(
        {
            "timeout": timeout,
            "offset": offset,
            # Only messages matter; ignore edits, callbacks and channel posts.
            "allowed_updates": json.dumps(["message"]),
        }
    )
    url = (
        f"https://api.telegram.org/bot{settings.telegram_bot_token}"
        f"/getUpdates?{query}"
    )
    req = request.Request(url, method="GET")
    try:
        # Socket timeout must outlive the long poll or every poll raises.
        with request.urlopen(req, timeout=timeout + 15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        _LOG.warning("getUpdates failed: %s: %s", type(exc).__name__, exc)
        return [], offset

    if not payload.get("ok"):
        _LOG.warning("getUpdates returned not-ok: %s", payload)
        return [], offset

    commands: list[TelegramCommand] = []
    next_offset = offset
    for update in payload.get("result", []):
        if not isinstance(update, dict):
            continue
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            continue
        # Acknowledge every update we saw, even ones we skip below, so a
        # non-text message cannot wedge the poll loop forever.
        next_offset = max(next_offset, update_id + 1)
        command = _parse_message(update)
        if command is None:
            continue
        if not _is_authorized(settings, command.chat_id):
            _LOG.warning("ignoring message from unauthorized chat %s", command.chat_id)
            continue
        commands.append(command)
    return commands, next_offset


def _parse_message(update: dict[str, object]) -> TelegramCommand | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None
    chat_id = chat.get("id")
    if chat_id is None:
        return None
    sender = message.get("from")
    username = ""
    if isinstance(sender, dict):
        username = str(sender.get("username") or sender.get("first_name") or "")
    return TelegramCommand(
        update_id=int(update["update_id"]),  # validated by the caller
        chat_id=str(chat_id),
        text=text.strip(),
        from_user=username,
    )


def _is_authorized(settings: Settings, chat_id: str) -> bool:
    """Only the configured chat may drive the bot.

    Bot tokens are reachable by anyone who learns the bot's name, so without
    this check a stranger could rewrite the project focus and task list.
    """
    expected = (settings.telegram_chat_id or "").strip()
    return bool(expected) and chat_id == expected
