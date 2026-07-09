from __future__ import annotations

import argparse
from datetime import datetime
from html import escape
import sys
from zoneinfo import ZoneInfo

from daily_intel_bot.briefing import persist_briefing_state
from daily_intel_bot.config import load_settings
from daily_intel_bot.obs import get_logger, setup_logging
from daily_intel_bot.persona import (
    select_persona,
    select_persona_image,
    select_persona_image_url,
)
from daily_intel_bot.pipeline import build_digest_text
from daily_intel_bot.state_store import StateStore
from daily_intel_bot.telegram_client import send_message, send_photo


def build_sample_digest(timezone: str) -> str:
    now = datetime.now(ZoneInfo(timezone))
    stamp = now.strftime("%Y-%m-%d %H:%M")
    return "\n".join(
        [
            f"Clawbot Daily Intel Sample",
            f"Generated: {stamp} {timezone}",
            "",
            "Top Tech Signals",
            "- Direct Telegram delivery path is working.",
            "",
            "Top AI Signals",
            "- OpenClaw gateway can stay for scheduling and control.",
            "",
            "Global Stats Snapshot",
            "- Collector modules are the next build step.",
        ]
    )


def _require_telegram(settings) -> None:
    """Fail fast (exit 2) if delivery credentials are missing."""
    missing = [
        name
        for name, value in (
            ("TELEGRAM_BOT_TOKEN", settings.telegram_bot_token),
            ("TELEGRAM_CHAT_ID", settings.telegram_chat_id),
        )
        if not value
    ]
    if missing:
        get_logger("main").error(
            "missing required env for delivery: %s", ", ".join(missing)
        )
        print(f"error: missing required env: {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)


def main() -> None:
    _configure_stdout()
    setup_logging()

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--send-test",
        action="store_true",
        help="Send a short Telegram test message directly via Bot API.",
    )
    parser.add_argument(
        "--send-sample-digest",
        action="store_true",
        help="Send a sample digest directly via Bot API.",
    )
    parser.add_argument(
        "--print-digest",
        action="store_true",
        help="Print the real digest built from live public sources.",
    )
    parser.add_argument(
        "--send-digest",
        action="store_true",
        help="Send the real digest built from live public sources.",
    )
    args = parser.parse_args()

    settings = load_settings()

    if args.send_test or args.send_sample_digest or args.send_digest:
        _require_telegram(settings)

    if args.send_test:
        result = send_message(settings, "Direct Telegram test message from Clawbot")
        print("direct-telegram-send=ok")
        print(f"message_id={result['result']['message_id']}")
        return

    if args.send_sample_digest:
        digest = build_sample_digest(settings.timezone)
        result = send_message(settings, digest)
        print("direct-telegram-digest=ok")
        print(f"message_id={result['result']['message_id']}")
        return

    if args.print_digest:
        digest, _bundle = build_digest_text(settings)
        print(digest)
        return

    if args.send_digest:
        digest, bundle = build_digest_text(settings)
        photo_result = _send_persona_photo_if_enabled(settings)
        if photo_result:
            print("persona-image=ok")
            print(f"photo_message_id={photo_result['result']['message_id']}")
        result = send_message(settings, digest)
        StateStore(settings.state_db_path).mark_sent(bundle.selected_items)
        if settings.briefing_mode == "dev_ielts":
            persist_briefing_state(settings)
        print("direct-live-digest=ok")
        if "message_ids" in result:
            print(f"message_ids={','.join(str(value) for value in result['message_ids'])}")
        print(f"message_id={result['result']['message_id']}")
        return

    print("daily-intel-bot scaffold ready")
    print(f"briefing_mode={settings.briefing_mode}")
    print(f"briefing_location={settings.briefing_location}")
    print(f"current_project_focus={settings.current_project_focus}")
    print(f"enabled_categories={','.join(settings.enabled_categories)}")
    print(f"news_items_per_category={settings.news_items_per_category}")
    print(f"timezone={settings.timezone}")
    print(f"digest_language={settings.digest_language}")
    print(f"telegram_bot_token_set={bool(settings.telegram_bot_token)}")
    print(f"telegram_chat_id_set={bool(settings.telegram_chat_id)}")
    print(f"enabled_collectors={','.join(settings.enabled_collectors)}")
    print(f"enabled_rss_feeds={','.join(settings.enabled_rss_feeds)}")
    print(f"enabled_sections={','.join(settings.enabled_sections)}")
    print(f"hacker_news_limit={settings.hacker_news_limit}")
    print(f"rss_limit_per_feed={settings.rss_limit_per_feed}")
    print(f"tech_item_limit={settings.tech_item_limit}")
    print(f"ai_item_limit={settings.ai_item_limit}")
    print(f"watchlist_item_limit={settings.watchlist_item_limit}")
    print(f"state_db_path={settings.state_db_path}")
    print(f"tavily_enabled={settings.tavily_enabled}")
    print(f"tavily_api_key_set={bool(settings.tavily_api_key)}")
    print(f"openai_enabled={settings.openai_enabled}")
    print(f"openai_api_key_set={bool(settings.openai_api_key)}")
    print(f"openai_model={settings.openai_model}")
    print(f"bot_persona_enabled={settings.bot_persona_enabled}")
    print(f"bot_persona_rotation={settings.bot_persona_rotation}")
    print(f"bot_persona_pool={','.join(settings.bot_persona_pool)}")
    print(f"bot_persona_force={settings.bot_persona_force}")
    print(f"bot_persona_spice_level={settings.bot_persona_spice_level}")
    print(f"bot_persona_safe_mode={settings.bot_persona_safe_mode}")
    print(f"bot_persona_image_enabled={settings.bot_persona_image_enabled}")
    print(f"bot_persona_image_dir={settings.bot_persona_image_dir}")
    print(f"bot_persona_image_urls_path={settings.bot_persona_image_urls_path}")


def _configure_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass


def _send_persona_photo_if_enabled(settings) -> dict[str, object] | None:
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
        print("persona-image=skipped")
        return None
    caption = (
        f"{escape(persona.icon)} <b>{escape(persona.name)}</b> "
        f"opens today's briefing."
    )
    if image_url:
        try:
            return send_photo(settings, image_url, caption=caption)
        except Exception as exc:
            print(f"persona-image-url=failed ({type(exc).__name__})")
            if image_path is None:
                return None
    try:
        return send_photo(settings, image_path, caption=caption)
    except Exception as exc:
        print(f"persona-image=failed ({type(exc).__name__})")
        return None


if __name__ == "__main__":
    main()
