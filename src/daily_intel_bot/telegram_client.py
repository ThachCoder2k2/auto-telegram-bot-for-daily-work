from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from urllib import parse, request
import uuid
import re
from html import unescape

from daily_intel_bot.config import Settings


TELEGRAM_TEXT_LIMIT = 3900


def _record_health(settings: Settings, exc: Exception | None) -> None:
    """Log the delivery outcome without letting health break delivery."""
    try:
        from daily_intel_bot import health
        from daily_intel_bot.state_store import StateStore

        health.record(StateStore(settings.state_db_path), "telegram", exc)
    except Exception:  # noqa: BLE001
        pass


def send_message(settings: Settings, text: str) -> dict[str, object]:
    if not settings.telegram_bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is missing")
    if not settings.telegram_chat_id:
        raise ValueError("TELEGRAM_CHAT_ID is missing")

    results: list[dict[str, object]] = []
    try:
        for part in _split_html_message(text):
            results.append(_send_message_part(settings, part))
    except Exception as exc:
        _record_health(settings, exc)
        raise
    _record_health(settings, None)
    if not results:
        raise ValueError("Message text is empty")
    if len(results) == 1:
        return results[0]
    results[-1]["message_ids"] = [
        result["result"]["message_id"]
        for result in results
        if isinstance(result.get("result"), dict)
    ]
    return results[-1]


def _send_message_part(settings: Settings, text: str) -> dict[str, object]:
    payload = parse.urlencode(
        {
            "chat_id": settings.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    url = (
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    )
    req = request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    with request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8")

    data = json.loads(body)
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data


def _split_html_message(text: str) -> list[str]:
    if _visible_len(text) <= TELEGRAM_TEXT_LIMIT:
        return [text]

    chunks: list[str] = []
    current = ""
    for section in text.split("\n----------------------------------------\n"):
        candidate = section if not current else f"{current}\n----------------------------------------\n{section}"
        if _visible_len(candidate) <= TELEGRAM_TEXT_LIMIT:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if _visible_len(section) <= TELEGRAM_TEXT_LIMIT:
            current = section
        else:
            chunks.extend(_split_plain_lines(section))
            current = ""
    if current:
        chunks.append(current)
    return chunks


def _split_plain_lines(text: str) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        candidate = line if not current else f"{current}\n{line}"
        if _visible_len(candidate) <= TELEGRAM_TEXT_LIMIT:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = line
    if current:
        chunks.append(current)
    return chunks


def _visible_len(text: str) -> int:
    without_tags = re.sub(r"<[^>]+>", "", text)
    return len(unescape(without_tags))


def send_photo(
    settings: Settings,
    photo_path: str | Path,
    caption: str | None = None,
) -> dict[str, object]:
    if not settings.telegram_bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is missing")
    if not settings.telegram_chat_id:
        raise ValueError("TELEGRAM_CHAT_ID is missing")

    if isinstance(photo_path, str) and photo_path.startswith(("http://", "https://")):
        return _send_photo_url(settings, photo_path, caption)

    path = Path(photo_path)
    if not path.exists():
        raise FileNotFoundError(f"Persona image not found: {path}")

    return _send_photo_file(settings, path, caption)


def _send_photo_url(
    settings: Settings,
    photo_url: str,
    caption: str | None,
) -> dict[str, object]:
    payload = {
        "chat_id": settings.telegram_chat_id,
        "photo": photo_url,
    }
    if caption:
        payload["caption"] = caption
        payload["parse_mode"] = "HTML"

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendPhoto"
    req = request.Request(
        url,
        data=parse.urlencode(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    with request.urlopen(req, timeout=30) as resp:
        response_body = resp.read().decode("utf-8")

    data = json.loads(response_body)
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data


def _send_photo_file(
    settings: Settings,
    path: Path,
    caption: str | None,
) -> dict[str, object]:
    boundary = f"----clawbot-{uuid.uuid4().hex}"
    fields = {
        "chat_id": settings.telegram_chat_id,
    }
    if caption:
        fields["caption"] = caption
        fields["parse_mode"] = "HTML"

    body = bytearray()
    for key, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(
                "utf-8"
            )
        )
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")

    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(
        (
            f'Content-Disposition: form-data; name="photo"; '
            f'filename="{path.name}"\r\n'
        ).encode("utf-8")
    )
    body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
    body.extend(path.read_bytes())
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendPhoto"
    req = request.Request(url, data=bytes(body), method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")

    with request.urlopen(req, timeout=30) as resp:
        response_body = resp.read().decode("utf-8")

    data = json.loads(response_body)
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data
