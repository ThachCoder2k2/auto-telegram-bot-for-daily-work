from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
from pathlib import Path

from daily_intel_bot.models import SignalItem


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
