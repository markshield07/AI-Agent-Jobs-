"""Upload a resume, store it, and turn it into facts."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from jobagent.answers import set_answers
from jobagent.config import Settings
from jobagent.db.database import transaction, utcnow
from jobagent.llm.backend import Completer, resolve_backend
from jobagent.resume import parser as resume_parser
from jobagent.resume.extract import content_sha, extract_text, suffix_of
from jobagent.resume.facts import add_facts


@dataclass(slots=True)
class IntakeResult:
    base_id: int
    filename: str
    fact_ids: list[int]
    already_uploaded: bool


def store_resume(
    conn: sqlite3.Connection, data: bytes, filename: str, settings: Settings
) -> tuple[int, str, bool]:
    """Write the file to disk and record it. Returns (base_id, text, already_uploaded).

    Re-uploading a byte-identical file returns the existing row rather than
    parsing it again — parsing costs a model call.
    """
    text = extract_text(data, filename)
    digest = content_sha(data)

    existing = conn.execute(
        "SELECT id FROM resume_base WHERE content_sha = ?", (digest,)
    ).fetchone()
    if existing:
        return int(existing["id"]), text, True

    settings.ensure_dirs()
    stored = settings.upload_dir / f"{digest[:16]}{suffix_of(filename)}"
    stored.write_bytes(data)

    with transaction(conn):
        cur = conn.execute(
            """INSERT INTO resume_base
                   (filename, stored_path, content_sha, parsed_text, uploaded_at)
               VALUES (?, ?, ?, ?, ?)""",
            (filename, str(stored), digest, text, utcnow()),
        )
    return int(cur.lastrowid), text, False


def ingest_resume(
    conn: sqlite3.Connection,
    data: bytes,
    filename: str,
    settings: Settings,
    completer: Completer | None = None,
) -> IntakeResult:
    """Store an uploaded resume and parse it into the fact base."""
    base_id, text, already = store_resume(conn, data, filename, settings)
    if already:
        return IntakeResult(base_id, filename, [], already_uploaded=True)

    parsed = resume_parser.parse_resume(text, completer=completer or resolve_backend(settings))
    fact_ids = add_facts(conn, resume_parser.to_facts(parsed, base_id=base_id))

    contact = resume_parser.contact_answers(parsed.contact)
    if contact:
        set_answers(conn, contact)

    return IntakeResult(base_id, filename, fact_ids, already_uploaded=False)


def latest_base(conn: sqlite3.Connection) -> dict[str, object] | None:
    row = conn.execute(
        "SELECT id, filename, stored_path, uploaded_at FROM resume_base ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "filename": row["filename"],
        "path": str(Path(row["stored_path"])),
        "uploaded_at": row["uploaded_at"],
    }
