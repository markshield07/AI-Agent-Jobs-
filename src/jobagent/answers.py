"""The answer bank: standing answers to application form questions.

Phase 1 only populates contact details from the parsed resume. Phase 4 fills in
work authorisation, salary and EEO, and reads from here when filling forms.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from jobagent.db.database import transaction, utcnow


def set_answer(
    conn: sqlite3.Connection,
    key: str,
    value: str,
    *,
    question_pattern: str | None = None,
    confidence: float = 1.0,
) -> None:
    with transaction(conn):
        conn.execute(
            """INSERT INTO answers (key, question_pattern, value, confidence, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   question_pattern = COALESCE(excluded.question_pattern, answers.question_pattern),
                   confidence = excluded.confidence,
                   updated_at = excluded.updated_at""",
            (key, question_pattern, value, confidence, utcnow()),
        )


def set_answers(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    for key, value in values.items():
        set_answer(conn, key, value)


def list_answers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM answers ORDER BY key").fetchall()
    return [{"key": r["key"], "value": r["value"], "confidence": r["confidence"]} for r in rows]
