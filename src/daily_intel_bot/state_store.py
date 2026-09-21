"""Durable memory for the bot.

Beyond the original send-dedupe table this now also backs the two-way command
loop (``meta`` offsets, ``task_events``), the feedback-aware ranker (``mutes``,
``item_feedback``) and IELTS spaced repetition (``vocab``). Every table is
created with ``IF NOT EXISTS`` so an existing database upgrades in place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from daily_intel_bot.models import SignalItem


# Leitner intervals in days, indexed by box - 1. A word answered correctly
# moves up a box; a miss drops it back to box 1 and returns tomorrow.
LEITNER_INTERVALS_DAYS = (1, 2, 4, 8, 16)
MAX_LEITNER_BOX = len(LEITNER_INTERVALS_DAYS)


@dataclass(frozen=True, slots=True)
class VocabEntry:
    word: str
    meaning: str
    example: str
    box: int
    correct: int
    wrong: int


@dataclass(frozen=True, slots=True)
class TaskEvent:
    day: str
    task: str
    status: str
    note: str


class StateStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sent_items (
                    item_key TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    last_sent_at TEXT NOT NULL
                )
                """
            )
            # Small key/value scratch space: Telegram getUpdates offset, last
            # recap date, and similar single-row bookkeeping.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            # Domains / keywords the user muted via /skip. The ranker drops or
            # penalises matching items.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mutes (
                    kind TEXT NOT NULL,
                    value TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (kind, value)
                )
                """
            )
            # Per-topic thumbs from /more and /less, used as a ranking prior.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS item_feedback (
                    topic TEXT PRIMARY KEY,
                    score REAL NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # IELTS vocabulary with Leitner scheduling.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vocab (
                    word TEXT PRIMARY KEY,
                    meaning TEXT NOT NULL,
                    example TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_reviewed TEXT,
                    due_at TEXT NOT NULL,
                    box INTEGER NOT NULL DEFAULT 1,
                    correct INTEGER NOT NULL DEFAULT 0,
                    wrong INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # Append-only log of /done, /blocked and /skip-task replies. Streak
            # and velocity are derived from this rather than stored, so a
            # corrected entry cannot leave a stale counter behind.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day TEXT NOT NULL,
                    task TEXT NOT NULL,
                    status TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            # How often a Notion task has been nudged. A high count against an
            # untouched task is the clearest signal that it is being avoided
            # rather than simply forgotten.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reminder_log (
                    page_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    count INTEGER NOT NULL DEFAULT 0,
                    first_reminded TEXT NOT NULL,
                    last_reminded TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_events_day ON task_events(day)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sent_items_at "
                "ON sent_items(last_sent_at)"
            )
            conn.commit()

    def recent_sent_keys(self, within_days: int = 3) -> set[str]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=within_days)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT item_key
                FROM sent_items
                WHERE last_sent_at >= ?
                """,
                (cutoff.isoformat(),),
            ).fetchall()
        return {row[0] for row in rows}

    def mark_sent(self, items: list[SignalItem]) -> None:
        sent_at = datetime.now(timezone.utc).isoformat()
        payload = [
            (
                item.key,
                item.source,
                item.topic,
                item.title,
                item.url,
                sent_at,
            )
            for item in items
        ]
        if not payload:
            return
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO sent_items (
                    item_key,
                    source,
                    topic,
                    title,
                    url,
                    last_sent_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_key) DO UPDATE SET
                    source = excluded.source,
                    topic = excluded.topic,
                    title = excluded.title,
                    url = excluded.url,
                    last_sent_at = excluded.last_sent_at
                """,
                payload,
            )
            conn.commit()

    # --- meta -------------------------------------------------------------

    def get_meta(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            conn.commit()

    # --- mutes ------------------------------------------------------------

    def add_mute(self, kind: str, value: str) -> None:
        """Mute a ``domain`` or ``keyword``. Re-muting is a no-op."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO mutes (kind, value, created_at)
                VALUES (?, ?, ?)
                """,
                (kind, value.strip().lower(), _now_iso()),
            )
            conn.commit()

    def remove_mute(self, value: str) -> int:
        """Unmute by value across both kinds. Returns rows removed."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM mutes WHERE value = ?", (value.strip().lower(),)
            )
            conn.commit()
            return cursor.rowcount

    def list_mutes(self) -> list[tuple[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT kind, value FROM mutes ORDER BY kind, value"
            ).fetchall()
        return [(row[0], row[1]) for row in rows]

    # --- topic feedback ---------------------------------------------------

    def bump_topic_feedback(self, topic: str, delta: float) -> float:
        """Nudge a topic's ranking prior, clamped to [-3, 3]."""
        current = self.topic_feedback().get(topic, 0.0)
        updated = max(-3.0, min(3.0, current + delta))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO item_feedback (topic, score, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(topic) DO UPDATE SET
                    score = excluded.score,
                    updated_at = excluded.updated_at
                """,
                (topic, updated, _now_iso()),
            )
            conn.commit()
        return updated

    def topic_feedback(self) -> dict[str, float]:
        with self._connect() as conn:
            rows = conn.execute("SELECT topic, score FROM item_feedback").fetchall()
        return {row[0]: float(row[1]) for row in rows}

    # --- trends -----------------------------------------------------------

    def topic_counts(self, within_days: int = 7) -> list[tuple[str, int]]:
        """Items sent per topic, most frequent first."""
        cutoff = _cutoff_iso(within_days)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT topic, COUNT(*) AS n
                FROM sent_items
                WHERE last_sent_at >= ?
                GROUP BY topic
                ORDER BY n DESC
                """,
                (cutoff,),
            ).fetchall()
        return [(row[0], int(row[1])) for row in rows]

    def domain_counts(self, within_days: int = 7) -> list[tuple[str, int]]:
        """Items sent per source domain, most frequent first."""
        cutoff = _cutoff_iso(within_days)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT url FROM sent_items WHERE last_sent_at >= ?",
                (cutoff,),
            ).fetchall()
        counts: dict[str, int] = {}
        for (url,) in rows:
            domain = _domain(url)
            if domain:
                counts[domain] = counts.get(domain, 0) + 1
        return sorted(counts.items(), key=lambda pair: pair[1], reverse=True)

    def recurring_terms(
        self,
        within_days: int = 7,
        min_hits: int = 3,
        limit: int = 5,
    ) -> list[tuple[str, int]]:
        """Headline words that keep resurfacing — a cheap topic-trend signal."""
        cutoff = _cutoff_iso(within_days)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT title FROM sent_items WHERE last_sent_at >= ?",
                (cutoff,),
            ).fetchall()
        counts: dict[str, int] = {}
        for (title,) in rows:
            # Count each term once per headline so a repeated word in one
            # title cannot manufacture a trend on its own.
            for term in {
                word
                for word in _tokenize(title)
                if len(word) > 3 and word not in _TREND_STOPWORDS
            }:
                counts[term] = counts.get(term, 0) + 1
        ranked = sorted(
            ((term, n) for term, n in counts.items() if n >= min_hits),
            key=lambda pair: pair[1],
            reverse=True,
        )
        return ranked[:limit]

    def sent_count(self, within_days: int = 7) -> int:
        cutoff = _cutoff_iso(within_days)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM sent_items WHERE last_sent_at >= ?",
                (cutoff,),
            ).fetchone()
        return int(row[0]) if row else 0

    # --- vocabulary (Leitner) --------------------------------------------

    def record_vocabulary(
        self,
        entries: list[tuple[str, str, str]],
        now: datetime | None = None,
    ) -> int:
        """Store today's words. Existing words keep their schedule."""
        now = _as_utc(now or datetime.now(timezone.utc))
        due = (now + timedelta(days=LEITNER_INTERVALS_DAYS[0])).isoformat()
        payload = [
            (word.strip().lower(), meaning.strip(), example.strip(), now.isoformat(), due)
            for word, meaning, example in entries
            if word.strip()
        ]
        if not payload:
            return 0
        with self._connect() as conn:
            cursor = conn.executemany(
                """
                INSERT OR IGNORE INTO vocab (
                    word, meaning, example, first_seen, due_at, box
                )
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                payload,
            )
            conn.commit()
            return cursor.rowcount

    def due_vocabulary(
        self,
        limit: int = 3,
        now: datetime | None = None,
    ) -> list[VocabEntry]:
        """Words whose Leitner interval has elapsed, oldest due first."""
        now = _as_utc(now or datetime.now(timezone.utc))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT word, meaning, example, box, correct, wrong
                FROM vocab
                WHERE due_at <= ?
                ORDER BY due_at ASC
                LIMIT ?
                """,
                (now.isoformat(), limit),
            ).fetchall()
        return [
            VocabEntry(
                word=row[0],
                meaning=row[1],
                example=row[2],
                box=int(row[3]),
                correct=int(row[4]),
                wrong=int(row[5]),
            )
            for row in rows
        ]

    def review_vocabulary(
        self,
        word: str,
        correct: bool,
        now: datetime | None = None,
    ) -> VocabEntry | None:
        """Apply a review result and reschedule the word."""
        now = _as_utc(now or datetime.now(timezone.utc))
        key = word.strip().lower()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT box, correct, wrong FROM vocab WHERE word = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            box, right, wrong = int(row[0]), int(row[1]), int(row[2])
            if correct:
                box = min(MAX_LEITNER_BOX, box + 1)
                right += 1
            else:
                box = 1
                wrong += 1
            due = now + timedelta(days=LEITNER_INTERVALS_DAYS[box - 1])
            conn.execute(
                """
                UPDATE vocab
                SET box = ?, correct = ?, wrong = ?,
                    last_reviewed = ?, due_at = ?
                WHERE word = ?
                """,
                (box, right, wrong, now.isoformat(), due.isoformat(), key),
            )
            conn.commit()
            meaning_row = conn.execute(
                "SELECT meaning, example FROM vocab WHERE word = ?", (key,)
            ).fetchone()
        return VocabEntry(
            word=key,
            meaning=meaning_row[0],
            example=meaning_row[1],
            box=box,
            correct=right,
            wrong=wrong,
        )

    def vocabulary_stats(self) -> tuple[int, int]:
        """(total words, words mastered at the top box)."""
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM vocab").fetchone()[0]
            mastered = conn.execute(
                "SELECT COUNT(*) FROM vocab WHERE box >= ?", (MAX_LEITNER_BOX,)
            ).fetchone()[0]
        return int(total), int(mastered)

    # --- reminder counts --------------------------------------------------

    def record_reminder(
        self,
        page_id: str,
        title: str,
        now: datetime | None = None,
    ) -> int:
        """Log a nudge and return how many times this task has been nudged."""
        stamp = _as_utc(now or datetime.now(timezone.utc)).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO reminder_log (
                    page_id, title, count, first_reminded, last_reminded
                )
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(page_id) DO UPDATE SET
                    count = count + 1,
                    title = excluded.title,
                    last_reminded = excluded.last_reminded
                """,
                (page_id, title, stamp, stamp),
            )
            conn.commit()
            row = conn.execute(
                "SELECT count FROM reminder_log WHERE page_id = ?", (page_id,)
            ).fetchone()
        return int(row[0]) if row else 1

    def reminder_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT page_id, count FROM reminder_log").fetchall()
        return {row[0]: int(row[1]) for row in rows}

    def clear_reminder_log(self, page_id: str) -> None:
        """Forget a task's nudge history, e.g. once it is completed."""
        with self._connect() as conn:
            conn.execute("DELETE FROM reminder_log WHERE page_id = ?", (page_id,))
            conn.commit()

    # --- task events ------------------------------------------------------

    def record_task_event(
        self,
        task: str,
        status: str,
        note: str = "",
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(timezone.utc)
        # ``day`` is deliberately the caller's calendar day, not UTC's: a
        # streak is about the user's days. Recording UTC meant a /done before
        # 07:00 in UTC+7 landed on yesterday and broke the streak.
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_events (day, task, status, note, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    now.date().isoformat(),
                    task,
                    status,
                    note,
                    _as_utc(now).isoformat(),
                ),
            )
            conn.commit()

    def done_days(self, within_days: int = 30, today: date | None = None) -> set[str]:
        """Distinct ISO dates that recorded at least one completed task.

        ``today`` should be the user's local date so the window lines up with
        the days stored by ``record_task_event``.
        """
        anchor = today or datetime.now(timezone.utc).date()
        cutoff = (anchor - timedelta(days=within_days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT day
                FROM task_events
                WHERE status = 'done' AND day >= ?
                """,
                (cutoff,),
            ).fetchall()
        return {row[0] for row in rows}

    def task_event_counts(
        self,
        within_days: int = 7,
        today: date | None = None,
    ) -> dict[str, int]:
        anchor = today or datetime.now(timezone.utc).date()
        cutoff = (anchor - timedelta(days=within_days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*)
                FROM task_events
                WHERE day >= ?
                GROUP BY status
                """,
                (cutoff,),
            ).fetchall()
        return {row[0]: int(row[1]) for row in rows}

    def recent_task_events(self, limit: int = 5) -> list[TaskEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT day, task, status, note
                FROM task_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            TaskEvent(day=row[0], task=row[1], status=row[2], note=row[3])
            for row in rows
        ]


def current_streak(done_days: set[str], today: date) -> int:
    """Consecutive days with a completed task, counting back from ``today``.

    Today not being logged yet is not a broken streak — it is a day still in
    progress — so counting starts at yesterday in that case.
    """
    cursor = today if today.isoformat() in done_days else today - timedelta(days=1)
    streak = 0
    while cursor.isoformat() in done_days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


_TREND_STOPWORDS = {
    "about",
    "after",
    "again",
    "against",
    "could",
    "from",
    "have",
    "into",
    "more",
    "most",
    "news",
    "over",
    "said",
    "says",
    "than",
    "that",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "under",
    "what",
    "when",
    "will",
    "with",
    "would",
    "your",
}


def _tokenize(text: str) -> list[str]:
    return [
        "".join(char for char in word if char.isalnum())
        for word in text.lower().split()
    ]


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def _as_utc(value: datetime) -> datetime:
    """Normalise any instant to UTC before it touches the database.

    Timestamps are compared as ISO strings in SQL, which compares characters,
    not moments. Mixing a UTC-stored value against a ``+07:00`` parameter makes
    those comparisons silently wrong whenever the offset flips the date. Naive
    values are assumed UTC, matching what the store has always written.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cutoff_iso(within_days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=within_days)).isoformat()
