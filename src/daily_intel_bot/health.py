"""What the bot knows about its own condition.

Two days of briefs went missing without a word: DNS was down inside the
container at send time, the scheduler burned its three retries in four
minutes, and nothing ever said so. Meanwhile hourly nudges kept arriving, so
from the outside the bot looked healthy.

Health is therefore reported from recorded outcomes — every outbound call is
logged with its result — rather than from configuration, which only ever says
what was intended.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.error import HTTPError

from daily_intel_bot.config import Settings
from daily_intel_bot.obs import get_logger
from daily_intel_bot.state_store import StateStore


_LOG = get_logger("health")

# Services worth a line in the report, in the order they matter.
TRACKED_SERVICES = ("telegram", "gemini", "openai", "notion", "tavily", "article")
LAST_DIGEST_META_KEY = "last_digest_sent_at"
# Above this share of rate-limited calls, quota is the story, not the symptom.
RATE_LIMIT_WARN_RATIO = 0.2


@dataclass(frozen=True, slots=True)
class ServiceHealth:
    name: str
    total: int
    failed: int
    rate_limited: int

    @property
    def ok(self) -> int:
        return self.total - self.failed

    @property
    def icon(self) -> str:
        if self.total == 0:
            return "⚪"
        if self.failed == 0:
            return "🟢"
        if self.failed == self.total:
            return "🔴"
        return "🟡"


def record(
    store: StateStore,
    service: str,
    exc: Exception | None = None,
) -> None:
    """Log one call's outcome. Never raises: health must not break the caller."""
    try:
        status = exc.code if isinstance(exc, HTTPError) else None
        detail = f"{type(exc).__name__}: {exc}" if exc else ""
        store.record_api_call(service, ok=exc is None, status=status, detail=detail)
    except Exception:  # noqa: BLE001 - a health write is never worth an outage
        _LOG.debug("could not record health for %s", service, exc_info=True)


def note_digest_sent(store: StateStore, now: datetime) -> None:
    store.set_meta(LAST_DIGEST_META_KEY, now.isoformat())


def hours_since_digest(store: StateStore, now: datetime) -> float | None:
    raw = store.get_meta(LAST_DIGEST_META_KEY, "")
    if not raw:
        return None
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() / 3600.0


def services(store: StateStore, within_hours: int = 24) -> list[ServiceHealth]:
    raw = store.api_health(within_hours=within_hours)
    return [
        ServiceHealth(
            name=name,
            total=raw[name]["total"],
            failed=raw[name]["failed"],
            rate_limited=raw[name]["rate_limited"],
        )
        for name in TRACKED_SERVICES
        if name in raw
    ]


def alerts(
    store: StateStore,
    settings: Settings,
    now: datetime,
    within_hours: int = 24,
) -> list[str]:
    """Problems worth interrupting the user about, in plain words."""
    found: list[str] = []

    since = hours_since_digest(store, now)
    if since is None:
        found.append("No daily brief has ever been delivered")
    elif since > 26:  # a day plus a little slack for a late run
        found.append(f"No daily brief for {int(since / 24)} day(s)")

    for service in services(store, within_hours):
        if service.total == 0:
            continue
        if service.failed == service.total:
            failure = store.last_api_failure(service.name)
            reason = f" — {failure[1]}" if failure else ""
            found.append(
                f"{service.name} failing on every call ({service.total}){reason}"
            )
        elif service.rate_limited / service.total >= RATE_LIMIT_WARN_RATIO:
            found.append(
                f"{service.name} rate-limited on "
                f"{service.rate_limited}/{service.total} calls — quota is the limit"
            )
    return found


def render_report(
    store: StateStore,
    settings: Settings,
    now: datetime,
    within_hours: int = 24,
) -> str:
    """The full /health answer."""
    lines = [f"🩺 <b>Health</b> · last {within_hours}h", ""]

    since = hours_since_digest(store, now)
    if since is None:
        lines.append("📭 <b>Daily brief:</b> never delivered")
    elif since < 26:
        lines.append(f"📰 <b>Daily brief:</b> {_ago(since)} ago")
    else:
        lines.append(f"🔴 <b>Daily brief:</b> {_ago(since)} ago — overdue")
    lines.append(
        f"⏰ <b>Next brief:</b> {settings.daily_send_hour:02d}:"
        f"{settings.daily_send_minute:02d} {settings.timezone.split('/')[-1]}"
    )

    stats = services(store, within_hours)
    if stats:
        lines.extend(["", "<b>Outbound calls</b>"])
        for service in stats:
            row = f"{service.icon} {service.name}: {service.ok}/{service.total} ok"
            if service.rate_limited:
                row += f" · {service.rate_limited} rate-limited"
            lines.append(row)
    else:
        lines.extend(["", "<i>No calls recorded yet</i>"])

    problems = alerts(store, settings, now, within_hours)
    if problems:
        lines.append("")
        lines.extend(f"⚠️ <i>{escape(problem)}</i>" for problem in problems)
    else:
        lines.extend(["", "✅ <i>Nothing wrong that the bot can see</i>"])
    return "\n".join(lines)


def _ago(hours: float) -> str:
    if hours < 1:
        return f"{int(hours * 60)}m"
    if hours < 48:
        return f"{int(hours)}h"
    return f"{int(hours / 24)}d"
