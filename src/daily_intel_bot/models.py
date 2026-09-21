from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class SignalItem:
    key: str
    topic: str
    source: str
    title: str
    url: str
    published_at: datetime | None
    score_hint: float = 0.0
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StatItem:
    key: str
    label: str
    value_text: str
    source: str
    url: str
    observed_at: datetime | None


@dataclass(slots=True)
class DigestBundle:
    tech_items: list[SignalItem]
    ai_items: list[SignalItem]
    watchlist_items: list[SignalItem]
    stats_items: list[StatItem]
    selected_items: list[SignalItem]
    # (word, meaning, example) triples to push into spaced repetition once the
    # brief has actually been delivered.
    vocabulary: tuple[tuple[str, str, str], ...] = ()
