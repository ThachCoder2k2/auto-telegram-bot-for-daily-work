from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(slots=True)
class Settings:
    briefing_mode: str
    briefing_location: str
    current_project_focus: str
    pending_tasks: tuple[str, ...]
    briefing_state_path: str
    enabled_categories: tuple[str, ...]
    news_items_per_category: int
    digest_language: str
    timezone: str
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    enabled_collectors: tuple[str, ...]
    enabled_rss_feeds: tuple[str, ...]
    enabled_sections: tuple[str, ...]
    hacker_news_limit: int
    rss_limit_per_feed: int
    tech_item_limit: int
    ai_item_limit: int
    watchlist_item_limit: int
    state_db_path: str
    tavily_api_key: str | None
    tavily_enabled: bool
    tavily_max_results: int
    tavily_search_depth: str
    tavily_topic: str
    tavily_time_range: str
    openai_api_key: str | None
    openai_enabled: bool
    openai_model: str
    bot_persona_enabled: bool
    bot_persona_rotation: str
    bot_persona_pool: tuple[str, ...]
    bot_persona_force: str
    bot_persona_spice_level: int
    bot_persona_safe_mode: bool
    bot_persona_image_enabled: bool
    bot_persona_image_dir: str
    bot_persona_image_urls_path: str


def load_settings() -> Settings:
    return Settings(
        briefing_mode=os.getenv("BRIEFING_MODE", "dev_ielts"),
        briefing_location=os.getenv("BRIEFING_LOCATION", "Hanoi, Vietnam"),
        current_project_focus=os.getenv(
            "CURRENT_PROJECT_FOCUS",
            "Godot horror-platformer",
        ),
        pending_tasks=_env_csv(
            "PENDING_TASKS",
            default=("Prototype one horror-platformer mechanic in Godot",),
        ),
        briefing_state_path=os.getenv(
            "BRIEFING_STATE_PATH",
            str(Path.cwd() / "state" / "briefing_state.json"),
        ),
        enabled_categories=_env_csv(
            "ENABLED_CATEGORIES",
            default=("web_tech", "hardware", "gaming", "governance"),
        ),
        news_items_per_category=_env_int("NEWS_ITEMS_PER_CATEGORY", default=5),
        digest_language=os.getenv("DIGEST_LANGUAGE", "en"),
        timezone=os.getenv("TIMEZONE", "Asia/Ho_Chi_Minh"),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
        enabled_collectors=_env_csv(
            "ENABLED_COLLECTORS",
            default=("hacker_news", "rss", "tavily", "world_bank"),
        ),
        enabled_rss_feeds=_env_csv(
            "ENABLED_RSS_FEEDS",
            default=("OpenAI News", "Hugging Face Blog"),
        ),
        enabled_sections=_env_csv(
            "ENABLED_SECTIONS",
            default=("tech", "ai", "stats", "watchlist"),
        ),
        hacker_news_limit=_env_int("HACKER_NEWS_LIMIT", default=18),
        rss_limit_per_feed=_env_int("RSS_LIMIT_PER_FEED", default=8),
        tech_item_limit=_env_int("TECH_ITEM_LIMIT", default=3),
        ai_item_limit=_env_int("AI_ITEM_LIMIT", default=3),
        watchlist_item_limit=_env_int("WATCHLIST_ITEM_LIMIT", default=3),
        state_db_path=os.getenv(
            "STATE_DB_PATH",
            str(Path.cwd() / "state" / "daily_intel.db"),
        ),
        tavily_api_key=os.getenv("TAVILY_API_KEY"),
        tavily_enabled=_env_flag("TAVILY_ENABLED", default=True),
        tavily_max_results=_env_int("TAVILY_MAX_RESULTS", default=4),
        tavily_search_depth=os.getenv("TAVILY_SEARCH_DEPTH", "basic"),
        tavily_topic=os.getenv("TAVILY_TOPIC", "news"),
        tavily_time_range=os.getenv("TAVILY_TIME_RANGE", "day"),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        openai_enabled=_env_flag("OPENAI_ENABLED", default=False),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
        bot_persona_enabled=_env_flag("BOT_PERSONA_ENABLED", default=False),
        bot_persona_rotation=os.getenv("BOT_PERSONA_ROTATION", "daily"),
        bot_persona_pool=_env_csv(
            "BOT_PERSONA_POOL",
            default=(
                "succubus",
                "milf_teacher",
                "soft_girlfriend",
                "rot_maiden",
                "final_boss_queen",
            ),
        ),
        bot_persona_force=os.getenv("BOT_PERSONA_FORCE", ""),
        bot_persona_spice_level=max(
            0,
            min(3, _env_int("BOT_PERSONA_SPICE_LEVEL", default=2)),
        ),
        bot_persona_safe_mode=_env_flag("BOT_PERSONA_SAFE_MODE", default=True),
        bot_persona_image_enabled=_env_flag(
            "BOT_PERSONA_IMAGE_ENABLED",
            default=True,
        ),
        bot_persona_image_dir=os.getenv(
            "BOT_PERSONA_IMAGE_DIR",
            str(Path.cwd() / "assets" / "personas"),
        ),
        bot_persona_image_urls_path=os.getenv(
            "BOT_PERSONA_IMAGE_URLS_PATH",
            str(Path.cwd() / "assets" / "personas" / "image_urls.json"),
        ),
    )


def _env_flag(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value.strip())
    except ValueError:
        return default


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    values = tuple(
        part.strip()
        for part in raw_value.split(",")
        if part.strip()
    )
    return values or default
