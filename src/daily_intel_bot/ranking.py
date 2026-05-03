from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

from daily_intel_bot.models import SignalItem


SOURCE_WEIGHTS = {
    "Hacker News": 1.0,
    "OpenAI News": 1.3,
    "Hugging Face Blog": 1.1,
}


def dedupe_items(items: list[SignalItem]) -> list[SignalItem]:
    chosen: dict[str, SignalItem] = {}
    for item in sorted(items, key=score_item, reverse=True):
        key = _dedupe_key(item)
        if key not in chosen:
            chosen[key] = item
    return list(chosen.values())


def score_item(item: SignalItem) -> float:
    score = item.score_hint + SOURCE_WEIGHTS.get(item.source, 0.8)
    if item.published_at:
        age_hours = max(
            0.0,
            (datetime.now(timezone.utc) - item.published_at).total_seconds()
            / 3600.0,
        )
        score += max(0.0, 36.0 - age_hours) / 18.0
    if item.topic == "ai":
        score += 0.3
    return score


def rank_items(items: list[SignalItem]) -> list[SignalItem]:
    unique_items = dedupe_items(items)
    return sorted(unique_items, key=score_item, reverse=True)


def select_with_recent_filter(
    ranked_items: list[SignalItem],
    recent_sent_keys: set[str],
    limit: int,
) -> list[SignalItem]:
    fresh = [item for item in ranked_items if item.key not in recent_sent_keys]
    if len(fresh) >= limit:
        return fresh[:limit]
    selected = fresh[:]
    for item in ranked_items:
        if item in selected:
            continue
        selected.append(item)
        if len(selected) >= limit:
            break
    return selected[:limit]


def _dedupe_key(item: SignalItem) -> str:
    parsed = urlparse(item.url)
    host = parsed.netloc.lower().replace("www.", "")
    path = parsed.path.rstrip("/").lower()
    title = " ".join(item.title.lower().split())
    return f"{host}|{path}|{title}"
