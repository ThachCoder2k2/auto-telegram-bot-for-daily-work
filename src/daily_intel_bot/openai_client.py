from __future__ import annotations

from dataclasses import dataclass
import json
from urllib import request


OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
USER_AGENT = "clawbot-daily-intel-telegram/0.1"


@dataclass(frozen=True, slots=True)
class AISentenceStructure:
    label: str
    sentence: str


@dataclass(frozen=True, slots=True)
class AIVocabularyEntry:
    word: str
    vietnamese_meaning: str
    example: str


@dataclass(frozen=True, slots=True)
class AIBriefingSections:
    daily_note: str
    game_idea: str
    game_feel: str
    prototype_task: str
    web_idea: str
    web_use: str
    sentence_structures: tuple[AISentenceStructure, ...]
    vocabulary: tuple[AIVocabularyEntry, ...]
    teacher_challenge: str
    answer_frame: str
    band8_phrase: str


def generate_ai_briefing_sections(
    api_key: str,
    model: str,
    context: dict[str, object],
) -> AIBriefingSections:
    body = {
        "model": model,
        "instructions": (
            "You are a digital curator and IELTS Band 8 mentor. Generate concise, "
            "fresh briefing sections from the provided news context only. Do not "
            "invent events, titles, links, or sources. The user will only see the "
            "news_items supplied here, so do not refer to any company, product, or "
            "event unless it appears in those items. Vocabulary examples must be "
            "about the user's Godot/web workflow or exact supplied news themes; "
            "they must not introduce unrelated layoffs, launches, lawsuits, tools, "
            "or market events. Keep a practical Hanoi developer perspective. Avoid "
            "Markdown and HTML. Keep game_idea and web_idea under 28 words each. "
            "Keep game_feel, prototype_task, and web_use under 22 words each. "
            "Each IELTS sentence structure must be one complete sentence under 28 "
            "words. Each vocabulary example must be under 18 words. If persona.enabled "
            "is true, follow persona.voice and persona.rules, using the requested "
            "address naturally. Keep adult persona flavor suggestive but non-graphic "
            "when persona.safe_mode is true. daily_note must feel like a normal "
            "short personal conversation from the persona, not a report headline. "
            "Do not wrap daily_note in quotation marks; Telegram will render it as "
            "a quote block."
        ),
        "input": json.dumps(context, ensure_ascii=False),
        "max_output_tokens": 1100,
        "text": {
            "verbosity": "low",
            "format": {
                "type": "json_schema",
                "name": "daily_intel_ai_sections",
                "strict": True,
                "schema": _response_schema(),
            },
        },
    }
    payload = json.dumps(body).encode("utf-8")
    req = request.Request(
        OPENAI_RESPONSES_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with request.urlopen(req, timeout=45) as resp:
        response_payload = json.loads(resp.read().decode("utf-8"))

    text = _extract_output_text(response_payload)
    if not text:
        raise ValueError("OpenAI response did not include output text")
    return _parse_sections(json.loads(text))


def _response_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "game_idea",
            "daily_note",
            "game_feel",
            "prototype_task",
            "web_idea",
            "web_use",
            "sentence_structures",
            "vocabulary",
            "teacher_challenge",
            "answer_frame",
            "band8_phrase",
        ],
        "properties": {
            "daily_note": {
                "type": "string",
                "description": (
                    "Two short conversational sentences from the persona to the user, "
                    "grounded in the pending task and today's visible news."
                ),
            },
            "game_idea": {
                "type": "string",
                "description": "One innovative Godot horror-platformer mechanic idea.",
            },
            "game_feel": {
                "type": "string",
                "description": "How the mechanic should feel to play.",
            },
            "prototype_task": {
                "type": "string",
                "description": "One tiny Godot prototype task the user can do next.",
            },
            "web_idea": {
                "type": "string",
                "description": "One UI/UX idea for a future digest dashboard.",
            },
            "web_use": {
                "type": "string",
                "description": "How the web idea helps the user's daily workflow.",
            },
            "sentence_structures": {
                "type": "array",
                "minItems": 3,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["label", "sentence"],
                    "properties": {
                        "label": {
                            "type": "string",
                            "enum": [
                                "Cleft sentence",
                                "Inversion",
                                "Complex conditional",
                            ],
                        },
                        "sentence": {"type": "string"},
                    },
                },
            },
            "vocabulary": {
                "type": "array",
                "minItems": 5,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["word", "vietnamese_meaning", "example"],
                    "properties": {
                        "word": {"type": "string"},
                        "vietnamese_meaning": {"type": "string"},
                        "example": {
                            "type": "string",
                            "description": "Example sentence in a game-dev context.",
                        },
                    },
                },
            },
            "teacher_challenge": {
                "type": "string",
                "description": "One IELTS Speaking Part 3 question based on the news.",
            },
            "answer_frame": {
                "type": "string",
                "description": "A concise IELTS answer frame the user can reuse.",
            },
            "band8_phrase": {
                "type": "string",
                "description": "One advanced phrase suitable for a Band 8 answer.",
            },
        },
    }


def _extract_output_text(payload: dict[str, object]) -> str:
    direct_text = payload.get("output_text")
    if isinstance(direct_text, str) and direct_text.strip():
        return direct_text.strip()

    chunks: list[str] = []
    output_items = payload.get("output")
    if not isinstance(output_items, list):
        return ""
    for item in output_items:
        if not isinstance(item, dict):
            continue
        content_items = item.get("content")
        if not isinstance(content_items, list):
            continue
        for content in content_items:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks).strip()


def _parse_sections(payload: dict[str, object]) -> AIBriefingSections:
    structures = payload.get("sentence_structures")
    vocabulary = payload.get("vocabulary")
    if not isinstance(structures, list) or not isinstance(vocabulary, list):
        raise ValueError("OpenAI response has invalid section arrays")

    return AIBriefingSections(
        daily_note=_required_string(payload, "daily_note"),
        game_idea=_required_string(payload, "game_idea"),
        game_feel=_required_string(payload, "game_feel"),
        prototype_task=_required_string(payload, "prototype_task"),
        web_idea=_required_string(payload, "web_idea"),
        web_use=_required_string(payload, "web_use"),
        sentence_structures=tuple(
            AISentenceStructure(
                label=_required_string(item, "label"),
                sentence=_required_string(item, "sentence"),
            )
            for item in structures[:3]
            if isinstance(item, dict)
        ),
        vocabulary=tuple(
            AIVocabularyEntry(
                word=_required_string(item, "word"),
                vietnamese_meaning=_required_string(item, "vietnamese_meaning"),
                example=_required_string(item, "example"),
            )
            for item in vocabulary[:5]
            if isinstance(item, dict)
        ),
        teacher_challenge=_required_string(payload, "teacher_challenge"),
        answer_frame=_required_string(payload, "answer_frame"),
        band8_phrase=_required_string(payload, "band8_phrase"),
    )


def _required_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"OpenAI response missing string field: {key}")
    return " ".join(value.split())
