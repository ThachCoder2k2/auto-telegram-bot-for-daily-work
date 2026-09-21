"""Google Gemini backend for AI briefing sections.

Mirrors ``openai_client.generate_ai_briefing_sections`` but talks to the Gemini
``generateContent`` REST endpoint. Returns the same ``AIBriefingSections`` so
the rest of the briefing code is provider-agnostic.
"""

from __future__ import annotations

import json
from urllib import request

from daily_intel_bot.enrich import TAKES_INSTRUCTIONS, takes_response_schema
from daily_intel_bot.openai_client import (
    AIBriefingSections,
    _parse_sections,
    _response_schema,
)


GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
USER_AGENT = "clawbot-daily-intel-telegram/0.1"

INSTRUCTIONS = (
    "You are a digital curator and IELTS Band 8 mentor. Generate concise, "
    "fresh briefing sections from the provided news context only. Do not "
    "invent events, titles, links, or sources. The user will only see the "
    "news_items supplied here, so do not refer to any company, product, or "
    "event unless it appears in those items. Vocabulary examples must be about "
    "the user's Godot/web workflow or exact supplied news themes; they must "
    "not introduce unrelated layoffs, launches, lawsuits, tools, or market "
    "events. Keep a practical Hanoi developer perspective. Avoid Markdown and "
    "HTML. Keep game_idea and web_idea under 28 words each. Keep game_feel, "
    "prototype_task, and web_use under 22 words each. Each IELTS sentence "
    "structure must be one complete sentence under 28 words. Each vocabulary "
    "example must be under 18 words. If persona.enabled is true, follow "
    "persona.voice and persona.rules, using the requested address naturally. "
    "Keep adult persona flavor suggestive but non-graphic when "
    "persona.safe_mode is true. daily_note must feel like a normal short "
    "personal conversation from the persona, not a report headline. Do not "
    "wrap daily_note in quotation marks; Telegram will render it as a quote "
    "block. Respond with a single JSON object matching the schema."
)


def generate_ai_briefing_sections_gemini(
    api_key: str,
    model: str,
    context: dict[str, object],
) -> AIBriefingSections:
    body = {
        "systemInstruction": {"parts": [{"text": INSTRUCTIONS}]},
        "contents": [
            {
                "role": "user",
                "parts": [{"text": json.dumps(context, ensure_ascii=False)}],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _gemini_schema(),
            # 2.5 models "think" by default, burning the output budget before
            # any JSON is emitted (MAX_TOKENS truncation). Disable it and keep a
            # generous cap for the structured payload.
            "thinkingConfig": {"thinkingBudget": 0},
            "maxOutputTokens": 2048,
            "temperature": 0.7,
        },
    }
    payload = json.dumps(body).encode("utf-8")
    url = f"{GEMINI_BASE_URL}/{model}:generateContent?key={api_key}"
    req = request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with request.urlopen(req, timeout=45) as resp:
        response_payload = json.loads(resp.read().decode("utf-8"))

    text = _extract_output_text(response_payload)
    if not text:
        raise ValueError("Gemini response did not include output text")
    return _parse_sections(json.loads(text))


def generate_item_takes_gemini(
    api_key: str,
    model: str,
    context: dict[str, object],
) -> dict[str, object]:
    """Ask Gemini for one 'why it matters' line per news item."""
    body = {
        "systemInstruction": {"parts": [{"text": TAKES_INSTRUCTIONS}]},
        "contents": [
            {
                "role": "user",
                "parts": [{"text": json.dumps(context, ensure_ascii=False)}],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _to_gemini(takes_response_schema()),
            "thinkingConfig": {"thinkingBudget": 0},
            "maxOutputTokens": 1024,
            "temperature": 0.4,
        },
    }
    payload = json.dumps(body).encode("utf-8")
    url = f"{GEMINI_BASE_URL}/{model}:generateContent?key={api_key}"
    req = request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with request.urlopen(req, timeout=45) as resp:
        response_payload = json.loads(resp.read().decode("utf-8"))

    text = _extract_output_text(response_payload)
    if not text:
        raise ValueError("Gemini response did not include output text")
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Gemini takes response was not a JSON object")
    return parsed


def _extract_output_text(payload: dict[str, object]) -> str:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return ""
    first = candidates[0]
    if not isinstance(first, dict):
        return ""
    content = first.get("content")
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "".join(chunks).strip()


def _gemini_schema() -> dict[str, object]:
    """Translate the OpenAI JSON schema into Gemini's responseSchema dialect.

    Gemini uses uppercase OpenAPI types and rejects ``additionalProperties``.
    """
    return _to_gemini(_response_schema())


_TYPE_MAP = {
    "object": "OBJECT",
    "array": "ARRAY",
    "string": "STRING",
    "integer": "INTEGER",
    "number": "NUMBER",
    "boolean": "BOOLEAN",
}


def _to_gemini(node: object) -> object:
    if not isinstance(node, dict):
        return node
    out: dict[str, object] = {}
    for key, value in node.items():
        if key == "additionalProperties":
            continue  # unsupported by Gemini responseSchema
        if key == "type" and isinstance(value, str):
            out["type"] = _TYPE_MAP.get(value.lower(), value.upper())
        elif key == "properties" and isinstance(value, dict):
            out["properties"] = {k: _to_gemini(v) for k, v in value.items()}
            # Preserve field order so Gemini emits a stable object shape.
            out["propertyOrdering"] = list(value.keys())
        elif key == "items":
            out["items"] = _to_gemini(value)
        else:
            out[key] = value
    return out
