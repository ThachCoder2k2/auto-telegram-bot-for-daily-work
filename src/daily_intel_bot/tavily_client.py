from __future__ import annotations

from dataclasses import dataclass
import json
from urllib import request


TAVILY_SEARCH_URL = "https://api.tavily.com/search"
USER_AGENT = "clawbot-daily-intel-telegram/0.1"


@dataclass(frozen=True, slots=True)
class TavilySearchSpec:
    query: str
    topic: str
    time_range: str
    search_depth: str
    max_results: int
    exclude_domains: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TavilyResult:
    title: str
    url: str
    content: str
    score: float
    published_date: str | None


def search_tavily(api_key: str, spec: TavilySearchSpec) -> list[TavilyResult]:
    body = {
        "query": spec.query,
        "topic": spec.topic,
        "time_range": spec.time_range,
        "search_depth": spec.search_depth,
        "max_results": spec.max_results,
    }
    if spec.exclude_domains:
        body["exclude_domains"] = list(spec.exclude_domains)

    payload = json.dumps(body).encode("utf-8")
    req = request.Request(
        TAVILY_SEARCH_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with request.urlopen(req, timeout=30) as resp:
        response_payload = json.loads(resp.read().decode("utf-8"))

    results: list[TavilyResult] = []
    for entry in response_payload.get("results", []):
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        url = str(entry.get("url") or "").strip()
        if not title or not url:
            continue
        content = str(entry.get("content") or "").strip()
        published_date = entry.get("published_date")
        results.append(
            TavilyResult(
                title=title,
                url=url,
                content=content,
                score=float(entry.get("score") or 0.0),
                published_date=str(published_date).strip() if published_date else None,
            )
        )
    return results
