"""AI-written voice for nudges, in the day's persona.

The daily brief feels alive because the model writes it against that day's
actual material. Reminders were doing the opposite: two canned sentences per
persona, repeated verbatim forever, reacting to nothing. Same persona, same
words, whether a task was a day old or eleven weeks stale.

This module gives nudges the digest's mechanism — persona voice plus the real
board state — and keeps the canned lines only as the offline fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from daily_intel_bot.notion_client import NoteEntry, NotionTask
from daily_intel_bot.persona import PersonaProfile


@dataclass(frozen=True, slots=True)
class NudgeVoice:
    """What the model wrote for one nudge."""

    opening: str
    asides: dict[int, str]
    closing: str


NUDGE_INSTRUCTIONS = (
    "You are writing a short nudge to one developer, fully in character as the "
    "persona described in the payload. Follow persona.voice and address the "
    "user the way persona.address says. Keep adult persona flavour suggestive "
    "but non-graphic when persona.safe_mode is true.\n"
    "\n"
    "Write from the facts given and nothing else. Every item carries real "
    "numbers: how many days it has been open, how many times it has already "
    "been nudged, whether it was ever started, what fields are missing. Use "
    "them. A task open eleven weeks must not read the same as one opened "
    "yesterday, and the fourth nudge must not read like the first.\n"
    "\n"
    "opening: two or three sentences that set the mood and name the single "
    "most telling fact on the board. Do not list every item; the list follows "
    "your text.\n"
    "asides: at most one short clause per item, only where you have something "
    "worth saying — an observation, a challenge, a way in. Skip an item rather "
    "than filling space. Never restate the item's own title.\n"
    "closing: one line that lands. No sign-off, no emoji.\n"
    "\n"
    "Never invent a task, a date, or a number that is not in the payload. "
    "Never scold about something the payload does not show. Avoid Markdown "
    "and HTML. Keep the whole thing under 90 words."
)


def nudge_response_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "opening": {"type": "string"},
            "asides": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "line": {"type": "string"},
                    },
                    "required": ["id", "line"],
                    "additionalProperties": False,
                },
            },
            "closing": {"type": "string"},
        },
        "required": ["opening", "asides", "closing"],
        "additionalProperties": False,
    }


def persona_payload(
    persona: PersonaProfile | None,
    safe_mode: bool,
) -> dict[str, object]:
    if persona is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "name": persona.name,
        "address": persona.address,
        "role": persona.role,
        "voice": persona.voice,
        "safe_mode": safe_mode,
    }


def task_context(
    tasks: list[NotionTask],
    now: datetime,
    reminder_counts: dict[str, int],
    persona: PersonaProfile | None,
    safe_mode: bool,
) -> dict[str, object]:
    """Everything the model needs to say something specific about the board."""
    items = []
    for index, task in enumerate(tasks):
        items.append(
            {
                "id": index,
                "title": task.title,
                "status": task.status or "unknown",
                "priority": task.priority or "unset",
                "estimated_time": task.estimated_time or "unset",
                "days_open": task.age_days(now),
                "days_since_user_touched": task.idle_days(now),
                "times_nudged": reminder_counts.get(task.page_id, 1),
            }
        )
    return {
        "kind": "tasks",
        "local_time": now.strftime("%H:%M"),
        "weekday": now.strftime("%A"),
        "persona": persona_payload(persona, safe_mode),
        "items": items,
    }


def note_context(
    entries: list[NoteEntry],
    now: datetime,
    persona: PersonaProfile | None,
    safe_mode: bool,
) -> dict[str, object]:
    items = [
        {
            "id": index,
            "title": entry.title,
            "days_until": entry.days_until(now),
            "date": entry.date.strftime("%A %d %B") if entry.date else None,
            "attendees": list(entry.attendees),
            "notes": entry.notes[:200],
        }
        for index, entry in enumerate(entries)
    ]
    return {
        # Dated entries are not chores: the tone is a heads-up, never a
        # reprimand, because nothing here decays by being left alone.
        "kind": "dated_notes",
        "tone": "a heads-up about something on the calendar, not a scolding",
        "local_time": now.strftime("%H:%M"),
        "weekday": now.strftime("%A"),
        "persona": persona_payload(persona, safe_mode),
        "items": items,
    }


def parse_nudge_voice(payload: dict[str, object], count: int) -> NudgeVoice:
    opening = str(payload.get("opening") or "").strip()
    closing = str(payload.get("closing") or "").strip()
    asides: dict[int, str] = {}
    raw = payload.get("asides")
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            index = entry.get("id")
            line = entry.get("line")
            if isinstance(index, int) and isinstance(line, str) and line.strip():
                if 0 <= index < count:
                    asides[index] = line.strip()
    if not opening:
        raise ValueError("nudge voice had no opening")
    return NudgeVoice(opening=opening, asides=asides, closing=closing)
