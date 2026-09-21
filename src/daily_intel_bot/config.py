"""Application settings, backed by pydantic-settings.

Field names map 1:1 to UPPER_SNAKE env vars (case-insensitive). Types are
validated and coerced at load time, so a bad int / out-of-range value fails
loudly at startup instead of silently reverting to a default. CSV env values
(``a,b,c``) are parsed into tuples via a pre-validator.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# CSV env values are parsed by our own validator, so tell pydantic-settings not
# to attempt JSON decoding of these tuple fields first.
CsvTuple = Annotated[tuple[str, ...], NoDecode]


_CSV_FIELDS = (
    "pending_tasks",
    "enabled_categories",
    "enabled_collectors",
    "enabled_rss_feeds",
    "enabled_sections",
    "bot_persona_pool",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        # Local CLI runs read .env directly; real environment variables still
        # win, so the container (which injects them via compose) is unaffected.
        env_file=".env",
        env_file_encoding="utf-8",
    )

    briefing_mode: str = "dev_ielts"
    briefing_location: str = "Hanoi, Vietnam"
    current_project_focus: str = "Godot horror-platformer"
    pending_tasks: CsvTuple = (
        "Prototype one horror-platformer mechanic in Godot",
    )
    briefing_state_path: str = ""
    enabled_categories: CsvTuple = (
        "web_tech",
        "hardware",
        "gaming",
        "governance",
    )
    news_items_per_category: int = 5
    digest_language: str = "en"
    timezone: str = "Asia/Ho_Chi_Minh"
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    enabled_collectors: CsvTuple = (
        "hacker_news",
        "rss",
        "tavily",
        "world_bank",
    )
    enabled_rss_feeds: CsvTuple = ("OpenAI News", "Hugging Face Blog")
    enabled_sections: CsvTuple = ("tech", "ai", "stats", "watchlist")
    hacker_news_limit: int = 18
    rss_limit_per_feed: int = 8
    tech_item_limit: int = 3
    ai_item_limit: int = 3
    watchlist_item_limit: int = 3
    state_db_path: str = ""
    # How long an already-sent story stays suppressed.
    repeat_window_days: int = 5
    # Two-way command loop.
    commands_enabled: bool = True
    command_poll_seconds: int = 30
    # Weekly recap section; 6 = Sunday in Python's weekday numbering.
    weekly_recap_enabled: bool = True
    weekly_recap_weekday: int = 6
    vocab_review_enabled: bool = True
    vocab_review_limit: int = 3
    # Per-item "why it matters" enrichment via the AI backend.
    item_takes_enabled: bool = True
    item_takes_limit: int = 3
    # --- Notion tasks ---
    notion_enabled: bool = False
    notion_token: str | None = None
    notion_database_id: str = ""
    notion_task_limit: int = 5
    notion_write_enabled: bool = True
    notion_reminders_enabled: bool = True
    # Nudges are held outside these hours so an "Every hour" task cannot wake
    # the user at 03:00; a reminder that came due overnight fires at the open.
    notion_quiet_start: int = 8
    notion_quiet_end: int = 22
    # Used when Reminder is ticked but Reminder Frequency was left blank.
    # Set to "None" to require an explicit frequency instead.
    notion_default_frequency: str = "Every day"
    # How often the poller looks for due reminders. The finest frequency the
    # board offers is hourly, so checking more often only burns API calls.
    notion_reminder_check_seconds: int = 3600
    # Overrides for when schema auto-detection picks the wrong column.
    # --- Notion dated notes / meetings ---
    # A second board whose rows carry a date. Unlike tasks, nothing here rots
    # by being left alone — the date simply arrives — so it is surfaced in the
    # daily brief and pinged at most once a day, only as the date nears.
    notion_notes_enabled: bool = False
    notion_notes_database_id: str = ""
    notion_notes_lookahead_days: int = 14
    notion_notes_alert_days: int = 3
    notion_prop_status: str = ""
    notion_prop_priority: str = ""
    notion_prop_last_reminded: str = ""
    tavily_api_key: str | None = None
    tavily_enabled: bool = True
    tavily_max_results: int = 4
    tavily_search_depth: str = "basic"
    tavily_topic: str = "news"
    tavily_time_range: str = "day"
    openai_api_key: str | None = None
    openai_enabled: bool = False
    openai_model: str = "gpt-5.4-mini"
    ai_provider: str = "openai"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    bot_persona_enabled: bool = False
    bot_persona_rotation: str = "daily"
    bot_persona_pool: CsvTuple = (
        "succubus",
        "milf_teacher",
        "soft_girlfriend",
        "rot_maiden",
        "final_boss_queen",
    )
    bot_persona_force: str = ""
    bot_persona_spice_level: int = 2
    bot_persona_safe_mode: bool = True
    bot_persona_image_enabled: bool = True
    bot_persona_image_dir: str = ""
    bot_persona_image_urls_path: str = ""

    @field_validator(*_CSV_FIELDS, mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("ai_provider", mode="before")
    @classmethod
    def _normalize_provider(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("bot_persona_spice_level")
    @classmethod
    def _clamp_spice(cls, value: int) -> int:
        return max(0, min(3, value))

    @model_validator(mode="after")
    def _fill_default_paths(self) -> "Settings":
        cwd = Path.cwd()
        if not self.briefing_state_path:
            self.briefing_state_path = str(cwd / "state" / "briefing_state.json")
        if not self.state_db_path:
            self.state_db_path = str(cwd / "state" / "daily_intel.db")
        if not self.bot_persona_image_dir:
            self.bot_persona_image_dir = str(cwd / "assets" / "personas")
        if not self.bot_persona_image_urls_path:
            self.bot_persona_image_urls_path = str(
                cwd / "assets" / "personas" / "image_urls.json"
            )
        return self


def load_settings() -> Settings:
    return Settings()
