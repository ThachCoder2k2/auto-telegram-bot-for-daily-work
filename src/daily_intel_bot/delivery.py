"""Single place that turns a built brief into a delivered brief.

Both the CLI (``--send-digest``) and the command poller (``/digest``) go
through here, so the post-send bookkeeping — marking items sent, stamping the
briefing state, recording vocabulary — cannot drift between the two.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from daily_intel_bot import health
from daily_intel_bot.briefing import persist_briefing_state
from daily_intel_bot.config import Settings
from daily_intel_bot.obs import get_logger
from daily_intel_bot.persona import (
    select_persona,
    select_persona_image,
    select_persona_image_url,
)
from daily_intel_bot.pipeline import build_digest_text
from daily_intel_bot.state_store import StateStore
from daily_intel_bot.telegram_client import send_message, send_photo


_LOG = get_logger("delivery")


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    message_ids: list[int]
    photo_message_id: int | None
    items_sent: int
    vocabulary_added: int


def send_daily_digest(settings: Settings) -> DeliveryResult:
    """Build today's brief, send it, then commit everything it implies."""
    text, bundle = build_digest_text(settings)
    photo = _send_persona_photo_if_enabled(settings)
    result = send_message(settings, text)

    store = StateStore(settings.state_db_path)
    # Recorded only after a successful send, so 'no brief for two days' is
    # something the bot can notice rather than something the user discovers.
    health.note_digest_sent(store, datetime.now(ZoneInfo(settings.timezone)))
    store.prune_api_calls()
    store.mark_sent(bundle.selected_items)
    vocabulary_added = store.record_vocabulary(list(bundle.vocabulary))
    if settings.briefing_mode == "dev_ielts":
        persist_briefing_state(settings)

    message_ids = result.get("message_ids")
    if not isinstance(message_ids, list):
        inner = result.get("result")
        message_ids = (
            [inner["message_id"]]
            if isinstance(inner, dict) and "message_id" in inner
            else []
        )
    photo_id = None
    if isinstance(photo, dict) and isinstance(photo.get("result"), dict):
        photo_id = photo["result"].get("message_id")

    return DeliveryResult(
        message_ids=[int(value) for value in message_ids],
        photo_message_id=photo_id,
        items_sent=len(bundle.selected_items),
        vocabulary_added=vocabulary_added,
    )


def _send_persona_photo_if_enabled(settings: Settings) -> dict[str, object] | None:
    if (
        settings.briefing_mode != "dev_ielts"
        or not settings.bot_persona_enabled
        or not settings.bot_persona_image_enabled
    ):
        return None
    now = datetime.now(ZoneInfo(settings.timezone))
    persona = select_persona(
        enabled=settings.bot_persona_enabled,
        rotation=settings.bot_persona_rotation,
        pool=settings.bot_persona_pool,
        forced_key=settings.bot_persona_force,
        now=now,
    )
    image_url = select_persona_image_url(
        persona,
        settings.bot_persona_image_urls_path,
        now,
    )
    image_path = select_persona_image(persona, settings.bot_persona_image_dir, now)
    if persona is None or (image_url is None and image_path is None):
        _LOG.info("persona image skipped: nothing configured for today")
        return None

    caption = (
        f"{escape(persona.icon)} <b>{escape(persona.name)}</b> "
        f"opens today's briefing."
    )
    if image_url:
        try:
            return send_photo(settings, image_url, caption=caption)
        except Exception as exc:  # noqa: BLE001 - image is decorative
            _LOG.warning("persona image url failed: %s", type(exc).__name__)
            if image_path is None:
                return None
    try:
        return send_photo(settings, image_path, caption=caption)
    except Exception as exc:  # noqa: BLE001 - image is decorative
        _LOG.warning("persona image file failed: %s", type(exc).__name__)
        return None
