"""Per-item enrichment: fetch the article, ask the AI why it matters.

The brief used to show a headline and, for the first item only, a scraped
snippet that was often navigation boilerplate. This module pulls real body text
and turns it into one sentence tied to the user's current project, so each top
item carries a reason to care instead of just a link.

Failures are absorbed on purpose: a slow outlet or a rate-limited model must
degrade the brief, never block it.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib import error, request

from bs4 import BeautifulSoup

from daily_intel_bot.obs import get_logger


_LOG = get_logger("enrich")

USER_AGENT = "clawbot-daily-intel-telegram/0.1"
ARTICLE_TIMEOUT_SECONDS = 12
# Enough body text for the model to judge relevance without paying for a
# whole long-read.
MAX_ARTICLE_CHARS = 1800
MAX_FETCH_BYTES = 800_000

_SKIP_TAGS = ("script", "style", "nav", "header", "footer", "aside", "form")


@dataclass(frozen=True, slots=True)
class EnrichmentRequest:
    url: str
    title: str
    summary: str
    category: str


def fetch_article_text(url: str) -> str:
    """Return readable body text for ``url``, or "" if it cannot be had."""
    req = request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with request.urlopen(req, timeout=ARTICLE_TIMEOUT_SECONDS) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "html" not in content_type.lower():
                return ""
            raw = resp.read(MAX_FETCH_BYTES)
    except (error.URLError, TimeoutError, OSError, ValueError) as exc:
        _LOG.info("article fetch failed for %s: %s", url, type(exc).__name__)
        return ""

    try:
        soup = BeautifulSoup(raw, "lxml")
    except Exception:  # noqa: BLE001 - malformed markup is not worth a traceback
        return ""

    for tag in soup(_SKIP_TAGS):
        tag.decompose()

    paragraphs = [
        text
        for text in (node.get_text(" ", strip=True) for node in soup.find_all("p"))
        # Short <p> blocks are almost always captions, bylines or cookie notices.
        if len(text) > 80
    ]
    return " ".join(paragraphs)[:MAX_ARTICLE_CHARS]


def build_take_context(
    requests: list[EnrichmentRequest],
    focus: str,
    tasks: list[str],
    location: str,
) -> dict[str, object]:
    """Assemble the model payload, fetching body text for each item."""
    items = []
    for index, item in enumerate(requests):
        body = fetch_article_text(item.url)
        items.append(
            {
                "id": index,
                "category": item.category,
                "title": item.title,
                # Prefer real body text; fall back to whatever the search API
                # gave us rather than sending an empty excerpt.
                "excerpt": body or item.summary[:400],
            }
        )
    return {
        "current_project_focus": focus,
        "pending_tasks": tasks,
        "location": location,
        "items": items,
    }


def takes_response_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "takes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "why_it_matters": {"type": "string"},
                    },
                    "required": ["id", "why_it_matters"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["takes"],
        "additionalProperties": False,
    }


TAKES_INSTRUCTIONS = (
    "You explain what a news item means for one solo developer who builds "
    "games and web tools. For each item, write a single sentence under 24 "
    "words stating a concrete implication: a decision to make, a risk to "
    "watch, a technique to borrow, or a tool worth trying. Prefer a link to "
    "their current_project_focus or pending_tasks when one genuinely exists. "
    "When the item does not touch their project, give the practical takeaway "
    "for a solo developer anyway — never answer that it is irrelevant, and "
    "never restate the headline. Ground every claim in the excerpt; invent "
    "nothing. No Markdown, no HTML, no quotation marks. Return one entry per "
    "input id."
)


def parse_takes(payload: dict[str, object], count: int) -> dict[int, str]:
    """Map the model's response back onto item indices."""
    takes = payload.get("takes")
    if not isinstance(takes, list):
        return {}
    result: dict[int, str] = {}
    for entry in takes:
        if not isinstance(entry, dict):
            continue
        index = entry.get("id")
        text = entry.get("why_it_matters")
        if not isinstance(index, int) or not isinstance(text, str):
            continue
        if 0 <= index < count and text.strip():
            result[index] = text.strip()
    return result
