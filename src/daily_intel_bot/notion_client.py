"""Notion tasks: read the board, and write back when work is done or reminded.

Notion split databases from data sources in API version 2025-09-03, so a
database id alone cannot be queried any more — it has to be resolved to a data
source id first. That lookup is cached in the ``meta`` table because the
mapping only changes when the board itself is restructured.

Property names are detected from the live schema rather than hard-coded, with
env overrides for the cases where detection guesses wrong. Renaming a column in
Notion should not silently empty the section.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from urllib import error, parse, request

from daily_intel_bot.config import Settings
from daily_intel_bot.obs import get_logger, is_transient_http_error, with_retries


_LOG = get_logger("notion")

NOTION_API = "https://api.notion.com/v1"
# Pinned deliberately: Notion requires an explicit version and silently
# changes behaviour between them.
NOTION_VERSION = "2026-03-11"
DATA_SOURCE_META_KEY = "notion_data_source_id"
REQUEST_TIMEOUT_SECONDS = 25


class NotionError(RuntimeError):
    """A Notion API call failed in a way the caller should surface."""


@dataclass(frozen=True, slots=True)
class NotionSchema:
    """Which property name plays which role on this board."""

    title: str
    status: str = ""
    priority: str = ""
    estimated_time: str = ""
    reminder: str = ""
    reminder_frequency: str = ""
    last_reminded: str = ""
    status_is_checkbox: bool = False
    done_option: str = "Done"


@dataclass(frozen=True, slots=True)
class NotionTask:
    page_id: str
    title: str
    status: str
    priority: str
    estimated_time: str
    reminder: bool
    reminder_frequency: str
    last_reminded: datetime | None
    url: str
    # Notion maintains these on every page; they are what makes "this has sat
    # untouched for six days" possible to say.
    created_at: datetime | None = None
    edited_at: datetime | None = None

    @property
    def is_done(self) -> bool:
        return self.status.strip().lower() == "done"

    def idle_days(self, now: datetime) -> int | None:
        """Days since anyone last edited the task."""
        if self.edited_at is None:
            return None
        return max(0, (now - self.edited_at).days)

    def age_days(self, now: datetime) -> int | None:
        if self.created_at is None:
            return None
        return max(0, (now - self.created_at).days)


# --- transport ------------------------------------------------------------


def _call(
    settings: Settings,
    path: str,
    *,
    body: dict | None = None,
    method: str = "GET",
) -> dict:
    if not settings.notion_token:
        raise NotionError("NOTION_TOKEN is missing")

    def _once() -> dict:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        req = request.Request(
            f"{NOTION_API}{path}",
            data=payload,
            method=method,
            headers={
                "Authorization": f"Bearer {settings.notion_token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
        )
        with request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        return with_retries(
            _once,
            attempts=3,
            backoff=2.0,
            logger=_LOG,
            label=f"notion {method} {path}",
            should_retry=is_transient_http_error,
        )
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        if exc.code == 404:
            # By far the most common setup mistake, and the error message
            # Notion returns for it is not self-explanatory.
            raise NotionError(
                "404 from Notion: the token cannot see this database. "
                "Share the database with the integration "
                f"(Connections -> add). Detail: {detail}"
            ) from exc
        if exc.code in (401, 403):
            raise NotionError(
                f"{exc.code} from Notion: token invalid or missing update "
                f"capability. Detail: {detail}"
            ) from exc
        raise NotionError(f"{exc.code} from Notion: {detail}") from exc
    except (error.URLError, TimeoutError, OSError) as exc:
        raise NotionError(f"Notion unreachable: {type(exc).__name__}: {exc}") from exc


# --- discovery ------------------------------------------------------------


def resolve_data_source_id(settings: Settings, store=None) -> str:
    """Map the configured database id to its data source id, cached."""
    if store is not None:
        cached = store.get_meta(DATA_SOURCE_META_KEY, "")
        if cached:
            return cached

    database_id = (settings.notion_database_id or "").strip()
    if not database_id:
        raise NotionError("NOTION_DATABASE_ID is missing")

    payload = _call(settings, f"/databases/{parse.quote(database_id)}")
    sources = payload.get("data_sources")
    if not isinstance(sources, list) or not sources:
        raise NotionError("database returned no data_sources")
    data_source_id = str(sources[0].get("id") or "")
    if not data_source_id:
        raise NotionError("data source entry had no id")

    if store is not None:
        store.set_meta(DATA_SOURCE_META_KEY, data_source_id)
    return data_source_id


def fetch_schema(settings: Settings, data_source_id: str) -> NotionSchema:
    payload = _call(settings, f"/data_sources/{parse.quote(data_source_id)}")
    return detect_schema(payload.get("properties") or {}, settings)


def detect_schema(properties: dict, settings: Settings) -> NotionSchema:
    """Work out which column means what, honouring env overrides."""
    by_type: dict[str, list[str]] = {}
    for name, spec in properties.items():
        by_type.setdefault(str(spec.get("type")), []).append(name)

    def pick(kinds: tuple[str, ...], *hints: str) -> str:
        """First property of a matching type whose name contains a hint."""
        for kind in kinds:
            for name in by_type.get(kind, []):
                lowered = name.lower()
                if any(hint in lowered for hint in hints):
                    return name
        return ""

    title = (by_type.get("title") or [""])[0]
    status = settings.notion_prop_status or (by_type.get("status") or [""])[0]
    status_is_checkbox = False
    if not status:
        status = pick(("checkbox",), "done", "complete", "xong")
        status_is_checkbox = bool(status)

    done_option = "Done"
    spec = properties.get(status) or {}
    if spec.get("type") == "status":
        options = [o.get("name", "") for o in spec.get("status", {}).get("options", [])]
        # Prefer the board's own wording over a hard-coded "Done".
        for candidate in options:
            if candidate.strip().lower() in {"done", "complete", "completed"}:
                done_option = candidate
                break

    return NotionSchema(
        title=title,
        status=status,
        priority=settings.notion_prop_priority or pick(("select",), "priority", "ưu tiên"),
        estimated_time=pick(("select",), "estimat", "time", "thời gian"),
        reminder=pick(("checkbox",), "reminder", "nhắc"),
        reminder_frequency=pick(("select",), "frequency", "tần suất"),
        last_reminded=settings.notion_prop_last_reminded
        or pick(("date",), "remind", "nhắc"),
        status_is_checkbox=status_is_checkbox,
        done_option=done_option,
    )


# --- reading --------------------------------------------------------------


def fetch_tasks(
    settings: Settings,
    data_source_id: str,
    schema: NotionSchema,
    page_size: int = 100,
) -> list[NotionTask]:
    """Return every task that is not finished."""
    body: dict[str, object] = {"page_size": min(max(page_size, 1), 100)}
    if schema.status:
        body["filter"] = (
            {"property": schema.status, "checkbox": {"equals": False}}
            if schema.status_is_checkbox
            else {
                "property": schema.status,
                "status": {"does_not_equal": schema.done_option},
            }
        )
    payload = _call(
        settings,
        f"/data_sources/{parse.quote(data_source_id)}/query",
        body=body,
        method="POST",
    )
    tasks = [
        _parse_task(page, schema)
        for page in payload.get("results", [])
        if isinstance(page, dict)
    ]
    return [task for task in tasks if task.title and not task.is_done]


def _parse_task(page: dict, schema: NotionSchema) -> NotionTask:
    props = page.get("properties") or {}

    def text(name: str) -> str:
        spec = props.get(name) or {}
        kind = spec.get("type")
        if kind == "title":
            return "".join(
                part.get("plain_text", "") for part in spec.get("title", [])
            ).strip()
        if kind == "status":
            return str((spec.get("status") or {}).get("name") or "")
        if kind == "select":
            return str((spec.get("select") or {}).get("name") or "")
        return ""

    def flag(name: str) -> bool:
        spec = props.get(name) or {}
        return bool(spec.get("checkbox")) if spec.get("type") == "checkbox" else False

    def when(name: str) -> datetime | None:
        spec = props.get(name) or {}
        if spec.get("type") != "date":
            return None
        raw = (spec.get("date") or {}).get("start")
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    status = text(schema.status) if schema.status else ""
    if schema.status_is_checkbox:
        status = "Done" if flag(schema.status) else "Not Started"

    def timestamp(key: str) -> datetime | None:
        raw = page.get(key)
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    return NotionTask(
        page_id=str(page.get("id") or ""),
        title=text(schema.title),
        status=status,
        priority=text(schema.priority) if schema.priority else "",
        estimated_time=text(schema.estimated_time) if schema.estimated_time else "",
        reminder=flag(schema.reminder) if schema.reminder else False,
        reminder_frequency=(
            text(schema.reminder_frequency) if schema.reminder_frequency else ""
        ),
        last_reminded=when(schema.last_reminded) if schema.last_reminded else None,
        url=str(page.get("url") or ""),
        created_at=timestamp("created_time"),
        edited_at=timestamp("last_edited_time"),
    )


# --- writing --------------------------------------------------------------


def mark_task_done(
    settings: Settings,
    page_id: str,
    schema: NotionSchema,
) -> None:
    if not schema.status:
        raise NotionError("board has no status column to update")
    value = (
        {"checkbox": True}
        if schema.status_is_checkbox
        else {"status": {"name": schema.done_option}}
    )
    _call(
        settings,
        f"/pages/{parse.quote(page_id)}",
        body={"properties": {schema.status: value}},
        method="PATCH",
    )


def touch_last_reminded(
    settings: Settings,
    page_id: str,
    schema: NotionSchema,
    when: datetime,
) -> None:
    """Stamp the reminder time; without this the interval never advances."""
    if not schema.last_reminded:
        return
    _call(
        settings,
        f"/pages/{parse.quote(page_id)}",
        body={
            "properties": {
                schema.last_reminded: {"date": {"start": when.isoformat()}}
            }
        },
        method="PATCH",
    )
