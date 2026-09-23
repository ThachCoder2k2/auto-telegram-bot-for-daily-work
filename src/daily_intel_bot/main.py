from __future__ import annotations

import argparse
from datetime import datetime
import sys
from zoneinfo import ZoneInfo

from daily_intel_bot.config import load_settings
from daily_intel_bot.delivery import digest_sent_today, send_daily_digest
from daily_intel_bot.obs import get_logger, setup_logging
from daily_intel_bot.pipeline import build_digest_text
from daily_intel_bot.poller import run_command_loop
from daily_intel_bot.telegram_client import send_message


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
    parser.add_argument(
        "--if-missed",
        action="store_true",
        help="With --send-digest: skip if today's brief already went out.",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Run the Telegram command loop (/done, /status, /quiz, …).",
    )
    args = parser.parse_args()

    settings = load_settings()

    if args.send_test or args.send_sample_digest or args.send_digest or args.serve:
        _require_telegram(settings)

    if args.serve:
        run_command_loop(settings)
        return

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
        if args.if_missed and digest_sent_today(settings):
            print("digest-already-sent-today=1")
            return
        delivered = send_daily_digest(settings)
        if delivered.photo_message_id is not None:
            print(f"persona-image=ok photo_message_id={delivered.photo_message_id}")
        print("direct-live-digest=ok")
        print(f"message_ids={','.join(str(value) for value in delivered.message_ids)}")
        print(f"items_sent={delivered.items_sent}")
        print(f"vocabulary_added={delivered.vocabulary_added}")
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


if __name__ == "__main__":
    main()
