"""Glue between the Notion board, the SQLite store and the bot's surfaces.

Keeping this separate from ``notion_client`` means the HTTP layer stays
testable without a database, and the command handlers stay free of Notion
wire details.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json

from daily_intel_bot.config import Settings
from daily_intel_bot.notion_client import (
    NotionError,
    NotionSchema,
    NotionTask,
    fetch_schema,
    fetch_tasks,
    mark_task_done,
    resolve_data_source_id,
    touch_last_reminded,
)
from daily_intel_bot.obs import get_logger
from daily_intel_bot.reminders import sort_tasks
from daily_intel_bot.state_store import StateStore


_LOG = get_logger("notion_tasks")

# The numbered list the user last saw, so "/ndone 2" can mean something after
# the message has scrolled away.
TASK_INDEX_META_KEY = "notion_task_index"


@dataclass(frozen=True, slots=True)
class Board:
    schema: NotionSchema
    tasks: list[NotionTask]


def is_configured(settings: Settings) -> bool:
    return bool(
        settings.notion_enabled
        and settings.notion_token
        and settings.notion_database_id
    )


def load_board(settings: Settings, store: StateStore) -> Board:
    """Fetch the unfinished tasks, sorted for display."""
    data_source_id = resolve_data_source_id(settings, store)
    schema = fetch_schema(settings, data_source_id)
    tasks = fetch_tasks(settings, data_source_id, schema)
    return Board(schema=schema, tasks=sort_tasks(tasks))


def load_board_quietly(settings: Settings, store: StateStore) -> Board | None:
    """Same as ``load_board`` but never raises.

    Used on the digest path, where a Notion outage must cost the section and
    not the whole brief.
    """
    if not is_configured(settings):
        return None
    try:
        return load_board(settings, store)
    except NotionError as exc:
        _LOG.warning("Notion unavailable, skipping task block: %s", exc)
        return None


def remember_task_order(store: StateStore, tasks: list[NotionTask]) -> None:
    store.set_meta(
        TASK_INDEX_META_KEY,
        json.dumps([task.page_id for task in tasks]),
    )


def resolve_task_number(store: StateStore, number: int) -> str:
    """Map the 1-based number shown to the user back to a page id."""
    raw = store.get_meta(TASK_INDEX_META_KEY, "")
    try:
        page_ids = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        page_ids = []
    if not isinstance(page_ids, list) or not 1 <= number <= len(page_ids):
        return ""
    return str(page_ids[number - 1])


def complete_task(
    settings: Settings,
    schema: NotionSchema,
    page_id: str,
) -> None:
    if not settings.notion_write_enabled:
        raise NotionError("NOTION_WRITE_ENABLED is false; refusing to edit Notion")
    mark_task_done(settings, page_id, schema)


def stamp_reminded(
    settings: Settings,
    schema: NotionSchema,
    page_id: str,
    when: datetime,
) -> None:
    """Record that a nudge went out.

    Best-effort: a failed stamp means the task is nudged again next cycle,
    which is far better than dropping the reminder entirely.
    """
    if not settings.notion_write_enabled:
        return
    try:
        touch_last_reminded(settings, page_id, schema, when)
    except NotionError as exc:
        _LOG.warning("could not stamp Last Reminded on %s: %s", page_id, exc)
