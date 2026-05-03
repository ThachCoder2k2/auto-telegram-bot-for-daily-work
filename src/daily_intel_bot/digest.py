from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from daily_intel_bot.models import DigestBundle, SignalItem, StatItem


MAX_DIGEST_CHARS = 3900


def render_digest(
    bundle: DigestBundle,
    timezone_name: str,
    enabled_sections: tuple[str, ...] = ("tech", "ai", "stats", "watchlist"),
) -> str:
    now = datetime.now(ZoneInfo(timezone_name))
    lines: list[str] = [
        "Clawbot Daily Intel",
        f"Generated: {now.strftime('%Y-%m-%d %H:%M')} {timezone_name}",
    ]

    if "tech" in enabled_sections:
        lines.extend(["", "Top Tech Signals"])
        lines.extend(_render_signal_section(bundle.tech_items))
    if "ai" in enabled_sections:
        lines.extend(["", "Top AI Signals"])
        lines.extend(_render_signal_section(bundle.ai_items))
    if "stats" in enabled_sections:
        lines.extend(["", "Global Stats Snapshot"])
        lines.extend(_render_stat_section(bundle.stats_items))
    if "watchlist" in enabled_sections:
        lines.extend(["", "Watchlist"])
        lines.extend(_render_signal_section(bundle.watchlist_items))

    digest = "\n".join(lines).strip()
    if len(digest) <= MAX_DIGEST_CHARS:
        return digest

    trimmed_lines = lines[:]
    while len("\n".join(trimmed_lines)) > MAX_DIGEST_CHARS and len(trimmed_lines) > 20:
        trimmed_lines.pop()
    trimmed_lines.append("")
    trimmed_lines.append("Note: digest trimmed to fit Telegram message limits.")
    return "\n".join(trimmed_lines).strip()


def _render_signal_section(items: list[SignalItem]) -> list[str]:
    if not items:
        return ["- No strong signals selected for this section yet."]
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        summary = f" | {item.summary}" if item.summary else ""
        lines.append(f"{index}. {item.title}")
        lines.append(f"   {item.source}{summary}")
        lines.append(f"   {item.url}")
    return lines


def _render_stat_section(items: list[StatItem]) -> list[str]:
    if not items:
        return ["- No stats available right now."]
    return [f"- {item.label}: {item.value_text}" for item in items]
