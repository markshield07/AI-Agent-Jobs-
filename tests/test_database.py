from __future__ import annotations

import sqlite3

import pytest

from jobagent.db.database import transaction, utcnow


def test_wal_and_foreign_keys_are_on(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_schema_is_versioned_once(conn):
    from jobagent.db.database import init_db

    init_db(conn)  # re-applying must not add a second version row
    rows = conn.execute("SELECT version FROM schema_version").fetchall()
    assert [r["version"] for r in rows] == [1]


def test_transaction_rolls_back(conn):
    with pytest.raises(RuntimeError):
        with transaction(conn):
            conn.execute(
                "INSERT INTO never_claim (term, created_at) VALUES (?, ?)",
                ("kubernetes", utcnow()),
            )
            raise RuntimeError("boom")

    assert conn.execute("SELECT COUNT(*) AS n FROM never_claim").fetchone()["n"] == 0


def test_job_status_is_constrained(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO jobs (id, url, title, company, source, status, scraped_at)
               VALUES ('a', 'https://x/1', 'Eng', 'Acme', 'lever', 'nonsense', ?)""",
            (utcnow(),),
        )


def _seed_application(conn) -> int:
    conn.execute(
        """INSERT INTO jobs (id, url, title, company, source, scraped_at)
           VALUES ('a', 'https://x/1', 'Eng', 'Acme', 'lever', ?)""",
        (utcnow(),),
    )
    cur = conn.execute(
        "INSERT INTO applications (job_id, mode, submitted_at) VALUES ('a', 'auto', ?)",
        (utcnow(),),
    )
    return int(cur.lastrowid)


def test_status_is_derived_from_the_newest_event(conn):
    app_id = _seed_application(conn)
    status = conn.execute(
        "SELECT status FROM application_status WHERE application_id = ?", (app_id,)
    ).fetchone()["status"]
    assert status == "applied", "an application with no events reads as applied"

    for when, to_status in (
        ("2026-01-01T00:00:00+00:00", "screening"),
        ("2026-01-09T00:00:00+00:00", "interviewing"),
    ):
        conn.execute(
            """INSERT INTO application_events
                   (application_id, kind, to_status, source, created_at)
               VALUES (?, 'status_change', ?, 'email', ?)""",
            (app_id, to_status, when),
        )

    row = conn.execute(
        "SELECT status FROM application_status WHERE application_id = ?", (app_id,)
    ).fetchone()
    assert row["status"] == "interviewing"


def test_first_reply_is_the_earliest_email_event(conn):
    app_id = _seed_application(conn)
    for when in ("2026-02-10T09:00:00+00:00", "2026-02-03T09:00:00+00:00"):
        conn.execute(
            """INSERT INTO application_events (application_id, kind, note, source, created_at)
               VALUES (?, 'email', 'reply', 'email', ?)""",
            (app_id, when),
        )

    row = conn.execute(
        "SELECT first_reply_at FROM application_status WHERE application_id = ?", (app_id,)
    ).fetchone()
    assert row["first_reply_at"] == "2026-02-03T09:00:00+00:00"


def test_each_thread_gets_its_own_connection(db):
    """FastAPI runs sync handlers in a threadpool, so this is the production path."""
    import threading

    results: dict[str, object] = {}

    def insert() -> None:
        other = db.connection()
        results["same_object"] = other is db.connection()
        with transaction(other):
            other.execute(
                "INSERT INTO never_claim (term, created_at) VALUES (?, ?)",
                ("kubernetes", utcnow()),
            )
        results["ok"] = True

    worker = threading.Thread(target=insert)
    worker.start()
    worker.join()

    assert results.get("ok") is True, results
    assert results["same_object"] is True, "a thread reuses its own connection"
    assert db.connection().execute("SELECT COUNT(*) AS n FROM never_claim").fetchone()["n"] == 1, (
        "the write is visible from the original thread"
    )
