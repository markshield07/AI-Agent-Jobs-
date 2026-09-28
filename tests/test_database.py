from __future__ import annotations

import sqlite3

import pytest

from jobagent.db.database import transaction, utcnow


def test_wal_and_foreign_keys_are_on(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_schema_is_versioned_once(conn):
    from jobagent.db.database import SCHEMA_VERSION, init_db

    init_db(conn)  # re-applying must not add a second version row
    rows = conn.execute("SELECT version FROM schema_version").fetchall()
    assert [r["version"] for r in rows] == [SCHEMA_VERSION]


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


def test_init_db_migrates_a_version_one_database(tmp_path):
    """A database made before phase 3 gains the variant columns and records version 2."""
    from jobagent.db.database import SCHEMA_VERSION, connect, init_db

    conn = connect(tmp_path / "old.db")
    init_db(conn)
    conn.executescript(
        """
        DROP TABLE resume_variants;
        CREATE TABLE resume_variants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT REFERENCES jobs(id) ON DELETE CASCADE,
            base_id INTEGER REFERENCES resume_base(id) ON DELETE SET NULL,
            facts_used TEXT NOT NULL, keyword_coverage REAL, pdf_path TEXT,
            created_at TEXT NOT NULL
        );
        DELETE FROM schema_version;
        INSERT INTO schema_version (version, applied_at) VALUES (1, 'then');
        """
    )

    init_db(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(resume_variants)")}
    assert {"content", "cover_letter", "status", "issues", "attempts"} <= columns
    app_columns = {row[1] for row in conn.execute("PRAGMA table_info(applications)")}
    assert "created_at" in app_columns, "phase 4 added created_at to applications"
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "submission_attempts" in tables
    assert "inbox_messages" in tables, "phase 5 added the inbox log"
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == SCHEMA_VERSION

    init_db(conn)  # a second start is a no-op, not a duplicate-column error
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 4


def test_unsubmitted_application_status_follows_its_newest_attempt(conn):
    """Before submission the derived status is the newest attempt's outcome."""
    conn.execute(
        """INSERT INTO jobs (id, url, title, company, source, scraped_at)
           VALUES ('a', 'https://x/1', 'Eng', 'Acme', 'lever', ?)""",
        (utcnow(),),
    )
    cur = conn.execute("INSERT INTO applications (job_id, mode) VALUES ('a', 'dry_run')")
    app_id = int(cur.lastrowid)

    def status():
        return conn.execute(
            "SELECT status FROM application_status WHERE application_id = ?", (app_id,)
        ).fetchone()["status"]

    assert status() == "pending"
    for outcome in ("dry_run", "needs_input"):
        conn.execute(
            """INSERT INTO submission_attempts
                   (application_id, mode, outcome, started_at, ended_at)
               VALUES (?, 'dry_run', ?, ?, ?)""",
            (app_id, outcome, utcnow(), utcnow()),
        )
        assert status() == outcome

    conn.execute("UPDATE applications SET submitted_at = ? WHERE id = ?", (utcnow(), app_id))
    assert status() == "applied", "submitted_at outranks the attempt outcome"
    conn.execute(
        """INSERT INTO application_events (application_id, kind, to_status, source, created_at)
           VALUES (?, 'status_change', 'rejected', 'email', ?)""",
        (app_id, utcnow()),
    )
    assert status() == "rejected", "and a status event outranks both"
