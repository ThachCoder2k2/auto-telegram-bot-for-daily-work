from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
from urllib import request
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

from daily_intel_bot.config import Settings
from daily_intel_bot.models import SignalItem, StatItem
from daily_intel_bot.obs import get_logger, with_retries
from daily_intel_bot.tavily_client import TavilySearchSpec, search_tavily


USER_AGENT = "clawbot-daily-intel-telegram/0.1"
_LOG = get_logger("collectors")


@dataclass(frozen=True, slots=True)
class FeedSpec:
    name: str
    url: str
    topic: str
    source_weight: float


@dataclass(frozen=True, slots=True)
class TavilyQuerySpec:
    query: str
    signal_topic: str


RSS_FEEDS: tuple[FeedSpec, ...] = (
    FeedSpec(
        name="OpenAI News",
        url="https://openai.com/news/rss.xml",
        topic="ai",
        source_weight=1.4,
    ),
    FeedSpec(
        name="Hugging Face Blog",
        url="https://huggingface.co/blog/feed.xml",
        topic="ai",
        source_weight=1.2,
    ),
)

TAVILY_QUERY_SPECS: tuple[TavilyQuerySpec, ...] = (
    TavilyQuerySpec(
        query=(
            "important developer tools, cloud infrastructure, open source, "
            "and cybersecurity news"
        ),
        signal_topic="tech",
    ),
    TavilyQuerySpec(
        query=(
            "important AI model releases, agent tools, multimodal systems, "
            "and LLM research news"
        ),
        signal_topic="ai",
    ),
)

TAVILY_EXCLUDED_DOMAINS = (
    "openai.com",
    "huggingface.co",
    "news.ycombinator.com",
    "reddit.com",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
)


WORLD_BANK_INDICATORS = (
    {
        "code": "NY.GDP.MKTP.KD.ZG",
        "label": "World GDP growth",
        "formatter": lambda value: f"{value:.1f}%",
    },
    {
        "code": "IT.NET.USER.ZS",
        "label": "Internet users",
        "formatter": lambda value: f"{value:.1f}% of population",
    },
    {
        "code": "SP.POP.TOTL",
        "label": "World population",
        "formatter": lambda value: f"{value / 1_000_000_000:.2f}B",
    },
)


def collect_hacker_news(limit: int = 18) -> list[SignalItem]:
    ids = _fetch_json("https://hacker-news.firebaseio.com/v0/topstories.json")
    items: list[SignalItem] = []
    if not isinstance(ids, list):
        return items

    for story_id in ids[:limit]:
        payload = _fetch_json(
            f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json"
        )
        if not isinstance(payload, dict):
            continue
        if payload.get("type") != "story":
            continue
        title = str(payload.get("title", "")).strip()
        url = str(payload.get("url") or "").strip()
        if not title or not url:
            continue
        published_at = None
        if payload.get("time"):
            published_at = datetime.fromtimestamp(
                int(payload["time"]), tz=timezone.utc
            )
        topic = _infer_topic(title, url, default="tech")
        score = float(payload.get("score") or 0)
        comments = int(payload.get("descendants") or 0)
        summary = f"HN score {int(score)}, comments {comments}"
        items.append(
            SignalItem(
                key=_make_key("hacker-news", url, title),
                topic=topic,
                source="Hacker News",
                title=title,
                url=url,
                published_at=published_at,
                score_hint=min(score / 120.0, 3.0),
                summary=summary,
                metadata={
                    "comments": comments,
                    "score": score,
                },
            )
        )
    return items


def collect_rss_items(
    limit_per_feed: int = 8,
    enabled_feed_names: set[str] | None = None,
) -> list[SignalItem]:
    items: list[SignalItem] = []
    for feed in RSS_FEEDS:
        if (
            enabled_feed_names is not None
            and feed.name.lower() not in enabled_feed_names
        ):
            continue
        try:
            root = _fetch_xml(feed.url)
        except Exception:
            continue
        entries = _extract_feed_entries(root)
        for entry in entries[:limit_per_feed]:
            title = entry.get("title", "").strip()
            url = entry.get("url", "").strip()
            if not title or not url:
                continue
            published_at = _parse_datetime(entry.get("published"))
            topic = _infer_topic(title, url, default=feed.topic)
            items.append(
                SignalItem(
                    key=_make_key(feed.name, url, title),
                    topic=topic,
                    source=feed.name,
                    title=title,
                    url=url,
                    published_at=published_at,
                    score_hint=feed.source_weight,
                    summary=f"Recent post from {feed.name}",
                    metadata={"feed_url": feed.url},
                )
            )
    return items


def collect_tavily_items(
    settings: Settings,
    limit_per_query: int | None = None,
) -> list[SignalItem]:
    if not settings.tavily_enabled or not settings.tavily_api_key:
        return []

    max_results = limit_per_query or settings.tavily_max_results
    items: list[SignalItem] = []
    for spec in TAVILY_QUERY_SPECS:
        try:
            results = search_tavily(
                settings.tavily_api_key,
                TavilySearchSpec(
                    query=spec.query,
                    topic=settings.tavily_topic,
                    time_range=settings.tavily_time_range,
                    search_depth=settings.tavily_search_depth,
                    max_results=max_results,
                    exclude_domains=TAVILY_EXCLUDED_DOMAINS,
                ),
            )
        except Exception:
            continue

        for result in results:
            published_at = _parse_datetime(result.published_date)
            source = _domain_label(result.url)
            snippet = _truncate_summary(result.content, limit=180)
            summary_parts = []
            if snippet:
                summary_parts.append(snippet)
            summary_parts.append("via Tavily")
            items.append(
                SignalItem(
                    key=_make_key("tavily", result.url, result.title),
                    topic=spec.signal_topic,
                    source=source,
                    title=result.title,
                    url=result.url,
                    published_at=published_at,
                    score_hint=min(max(result.score, 0.0), 1.0) * 1.4,
                    summary=" | ".join(summary_parts),
                    metadata={
                        "query": spec.query,
                        "retrieval_source": "tavily",
                        "tavily_score": result.score,
                    },
                )
            )
    return items


def collect_world_bank_stats() -> list[StatItem]:
    stats: list[StatItem] = []
    for indicator in WORLD_BANK_INDICATORS:
        code = indicator["code"]
        url = (
            "https://api.worldbank.org/v2/country/WLD/indicator/"
            f"{code}?format=json&mrv=1"
        )
        payload = _fetch_json(url)
        if not isinstance(payload, list) or len(payload) < 2:
            continue
        rows = payload[1]
        if not isinstance(rows, list) or not rows:
            continue
        row = rows[0]
        value = row.get("value")
        if value is None:
            continue
        year = str(row.get("date") or "").strip()
        observed_at = _parse_year(year)
        formatter = indicator["formatter"]
        value_text = f"{formatter(float(value))} ({year})"
        stats.append(
            StatItem(
                key=_make_key("world-bank", code, indicator["label"]),
                label=indicator["label"],
                value_text=value_text,
                source="World Bank",
                url=url,
                observed_at=observed_at,
            )
        )
    return stats


def _fetch_json(url: str) -> object:
    def _do() -> object:
        req = request.Request(url, headers={"User-Agent": USER_AGENT})
        with request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    return with_retries(_do, attempts=3, logger=_LOG, label=f"GET {url}")


def _fetch_xml(url: str) -> ET.Element:
    def _do() -> ET.Element:
        req = request.Request(url, headers={"User-Agent": USER_AGENT})
        with request.urlopen(req, timeout=20) as resp:
            content = resp.read()
        return ET.fromstring(content)

    return with_retries(_do, attempts=3, logger=_LOG, label=f"GET {url}")


def _extract_feed_entries(root: ET.Element) -> list[dict[str, str]]:
    tag = _strip_namespace(root.tag)
    if tag == "rss":
        channel = root.find("channel")
        if channel is None:
            return []
        entries = []
        for item in channel.findall("item"):
            entries.append(
                {
                    "title": _text(item.find("title")),
                    "url": _text(item.find("link")),
                    "published": _text(item.find("pubDate")),
                }
            )
        return entries

    if tag == "feed":
        entries = []
        namespace = _namespace(root.tag)
        for entry in root.findall(f"{namespace}entry"):
            link = ""
            for candidate in entry.findall(f"{namespace}link"):
                href = candidate.attrib.get("href", "").strip()
                rel = candidate.attrib.get("rel", "alternate").strip()
                if href and rel in {"alternate", ""}:
                    link = href
                    break
            entries.append(
                {
                    "title": _text(entry.find(f"{namespace}title")),
                    "url": link,
                    "published": _text(entry.find(f"{namespace}published"))
                    or _text(entry.find(f"{namespace}updated")),
                }
            )
        return entries

    return []


def _make_key(source: str, url: str, title: str) -> str:
    payload = f"{source}|{url}|{title}".encode("utf-8")
    return hashlib.sha1(payload).hexdigest()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
        return parsed.astimezone(timezone.utc)
    except Exception:
        pass
    try:
        normalized = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _parse_year(value: str) -> datetime | None:
    if not value.isdigit():
        return None
    return datetime(int(value), 1, 1, tzinfo=timezone.utc)


def _infer_topic(title: str, url: str, default: str) -> str:
    haystack = f"{title} {url}".lower()
    ai_keywords = (
        "ai",
        "artificial intelligence",
        "llm",
        "model",
        "anthropic",
        "openai",
        "huggingface",
        "gemini",
        "transformer",
    )
    if any(keyword in haystack for keyword in ai_keywords):
        return "ai"
    return default


def _text(node: ET.Element | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def _strip_namespace(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _namespace(tag: str) -> str:
    if tag.startswith("{") and "}" in tag:
        return tag.split("}", 1)[0] + "}"
    return ""


def _domain_label(url: str) -> str:
    host = urlparse(url).netloc.lower().replace("www.", "")
    return host or "web"


def _truncate_summary(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        return ""
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."
