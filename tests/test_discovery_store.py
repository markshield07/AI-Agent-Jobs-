"""The jobs store: URL canonicalisation, dedupe on insert, queues, status and run rows."""

from __future__ import annotations

import pytest

from jobagent.discovery import store
from jobagent.discovery.models import RawJob
from jobagent.discovery.store import canonical_url, job_id, upsert_jobs

GH_URL = "https://boards.greenhouse.io/acme/jobs/123"


def _job(
    url: str = GH_URL,
    title: str = "Software Engineer",
    company: str = "Acme",
    source: str = "greenhouse",
    **extra,
) -> RawJob:
    return RawJob(url=url, title=title, company=company, source=source, **extra)


def _insert(conn, *jobs: RawJob, run_id: int | None = None) -> list[str]:
    return upsert_jobs(conn, jobs, run_id=run_id).new_ids


# --------------------------------------------------------- canonical_url --


def test_canonical_url_strips_tracking_params_and_fragment():
    url = f"{GH_URL}?gh_src=abc123&utm_source=linkedin&utm_medium=social&team=infra#content"
    assert canonical_url(url) == f"{GH_URL}?team=infra"


def test_canonical_url_strips_every_known_tracker():
    query = "&".join(f"{k}=1" for k in sorted(store._TRACKING_PARAMS))
    assert canonical_url(f"{GH_URL}?{query}") == GH_URL


def test_canonical_url_lowercases_scheme_and_host_but_not_path():
    assert (
        canonical_url("HTTPS://Jobs.Lever.co/Acme/ABC-123") == "https://jobs.lever.co/Acme/ABC-123"
    )


def test_canonical_url_normalises_trailing_slash():
    assert canonical_url("https://x.example/jobs/") == canonical_url("https://x.example/jobs")
    assert canonical_url("https://x.example/jobs/") == "https://x.example/jobs"
    assert canonical_url("https://x.example") == "https://x.example/"
    assert canonical_url("https://x.example/") == "https://x.example/"


def test_canonical_url_ignores_surrounding_whitespace():
    assert canonical_url(f"  {GH_URL}\n") == GH_URL


def test_canonical_url_keeps_query_order_and_blank_values():
    assert canonical_url("https://x.example/j?b=2&a=&c=3") == "https://x.example/j?b=2&a=&c=3"


# ---------------------------------------------------------------- job_id --


def test_job_id_is_stable_and_short():
    first = job_id(GH_URL, "Software Engineer", "Acme")
    assert first == job_id(GH_URL, "Software Engineer", "Acme")
    assert len(first) == 16
    int(first, 16)


def test_job_id_ignores_case_and_whitespace_of_title_and_company():
    base = job_id(GH_URL, "Software Engineer", "Acme")
    assert job_id(GH_URL, "  software ENGINEER ", " ACME\t") == base


def test_job_id_ignores_tracking_params_on_the_url():
    base = job_id(GH_URL, "Software Engineer", "Acme")
    assert job_id(f"{GH_URL}/?gh_src=xyz#top", "Software Engineer", "Acme") == base


def test_job_id_changes_with_title_company_or_url():
    base = job_id(GH_URL, "Software Engineer", "Acme")
    assert job_id(GH_URL, "Staff Engineer", "Acme") != base
    assert job_id(GH_URL, "Software Engineer", "Globex") != base
    assert job_id(f"{GH_URL}4", "Software Engineer", "Acme") != base


# ------------------------------------------------------------ upsert_jobs --


def test_upsert_inserts_a_new_job_with_canonical_url_and_pending_status(conn):
    job = _job(
        url=f"{GH_URL}/?utm_source=x",
        title="  Software Engineer ",
        company=" Acme ",
        location="Remote",
        description="Build things",
        apply_url=f"{GH_URL}#app",
        salary_min=100_000,
        salary_max=150_000,
        posted_at="2024-05-01T00:00:00+00:00",
        external_id="123",
    )
    result = upsert_jobs(conn, [job])

    assert result.new_ids == [job_id(job.url, job.title, job.company)]
    assert result.duplicates == 0 and result.rejected == 0

    row = store.get_job(conn, result.new_ids[0])
    assert row is not None
    assert row["url"] == GH_URL
    assert row["title"] == "Software Engineer"
    assert row["company"] == "Acme"
    assert row["source"] == "greenhouse"
    assert row["location"] == "Remote"
    assert row["description"] == "Build things"
    assert row["apply_url"] == f"{GH_URL}#app"
    assert (row["salary_min"], row["salary_max"]) == (100_000, 150_000)
    assert row["posted_at"] == "2024-05-01T00:00:00+00:00"
    assert row["external_id"] == "123"
    assert row["status"] == "pending"
    assert row["scraped_at"]
    assert row["run_id"] is None
    assert row["score"] is None and row["tier"] is None
    assert row["enriched_at"] is None and row["scored_at"] is None


def test_upsert_counts_the_same_posting_seen_twice_as_a_duplicate(conn):
    first = upsert_jobs(conn, [_job()])
    again = upsert_jobs(conn, [_job(url=f"{GH_URL}?gh_src=again", title="software engineer")])

    assert len(first.new_ids) == 1
    assert again.new_ids == [] and again.duplicates == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 1


def test_upsert_treats_same_url_with_a_different_title_as_a_duplicate(conn):
    upsert_jobs(conn, [_job(title="Software Engineer")])
    result = upsert_jobs(conn, [_job(title="Senior Software Engineer")])

    assert result.new_ids == []
    assert result.duplicates == 1
    rows = conn.execute("SELECT title FROM jobs").fetchall()
    assert [r["title"] for r in rows] == ["Software Engineer"], "the first listing wins"


def test_upsert_dedupes_within_one_batch(conn):
    result = upsert_jobs(conn, [_job(), _job(), _job(url="https://other.example/1")])
    assert len(result.new_ids) == 2
    assert result.duplicates == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"url": ""},
        {"url": "   "},
        {"title": ""},
        {"title": " \t"},
        {"company": ""},
        {"company": "  "},
    ],
)
def test_upsert_rejects_rows_missing_url_title_or_company(conn, bad):
    result = upsert_jobs(conn, [_job(**bad)])
    assert result.rejected == 1
    assert result.new_ids == [] and result.duplicates == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 0


def test_upsert_rejects_an_unparsable_url(conn):
    good, bad, also_good = (
        _job(url="https://x.example/1"),
        _job(url="https://[bad/x"),
        _job(url="https://x.example/2"),
    )
    with pytest.raises(ValueError):
        canonical_url(bad.url)

    result = upsert_jobs(conn, [good, bad, also_good])

    assert result.rejected == 1
    assert result.duplicates == 0
    assert len(result.new_ids) == 2, "the rest of the batch is kept"
    urls = {r["url"] for r in conn.execute("SELECT url FROM jobs").fetchall()}
    assert urls == {"https://x.example/1", "https://x.example/2"}


def test_upsert_rejects_a_missing_source_rather_than_counting_a_duplicate(conn):
    result = upsert_jobs(conn, [_job(source=None), _job(source="  ")])  # type: ignore[arg-type]
    assert result.rejected == 2
    assert result.duplicates == 0 and result.new_ids == []
    assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 0


@pytest.mark.parametrize("bad", [{"title": float("nan")}, {"company": 42}, {"url": None}])
def test_upsert_rejects_non_string_required_fields(conn, bad):
    result = upsert_jobs(conn, [_job(**bad), _job(url="https://x.example/ok")])
    assert result.rejected == 1
    assert len(result.new_ids) == 1


def test_upsert_stores_an_unreadable_remote_flag_as_unknown(conn):
    (jid,) = _insert(conn, _job(remote=float("nan")))  # type: ignore[arg-type]
    assert store.get_job(conn, jid)["remote"] is None


def test_upsert_mixed_batch_counts_each_outcome_once(conn):
    upsert_jobs(conn, [_job()])
    result = upsert_jobs(
        conn,
        [
            _job(),
            _job(url="https://new.example/1"),
            _job(url="https://new.example/2", title=""),
        ],
    )
    assert (len(result.new_ids), result.duplicates, result.rejected) == (1, 1, 1)


def test_upsert_detects_ats_from_the_apply_url_before_the_listing_url(conn):
    by_url, by_apply, unknown, given = _insert(
        conn,
        _job(),
        _job(url="https://careers.acme.example/x", apply_url="https://jobs.lever.co/acme/abc"),
        _job(url="https://careers.acme.example/y"),
        _job(url="https://careers.acme.example/z", ats_type="workday"),
    )
    assert store.get_job(conn, by_url)["ats_type"] == "greenhouse"
    assert store.get_job(conn, by_apply)["ats_type"] == "lever"
    assert store.get_job(conn, unknown)["ats_type"] is None
    assert store.get_job(conn, given)["ats_type"] == "workday"


def test_upsert_stores_remote_as_one_zero_or_null(conn):
    yes, no, unknown = _insert(
        conn,
        _job(url="https://x.example/1", remote=True),
        _job(url="https://x.example/2", remote=False),
        _job(url="https://x.example/3"),
    )
    raw = {r["id"]: r["remote"] for r in conn.execute("SELECT id, remote FROM jobs").fetchall()}
    assert raw == {yes: 1, no: 0, unknown: None}

    assert store.get_job(conn, yes)["remote"] is True
    assert store.get_job(conn, no)["remote"] is False
    assert store.get_job(conn, unknown)["remote"] is None


def test_upsert_records_the_run(conn):
    run_id = store.start_run(conn)
    (jid,) = _insert(conn, _job(), run_id=run_id)
    assert store.get_job(conn, jid)["run_id"] == run_id


def test_get_job_returns_none_for_an_unknown_id(conn):
    assert store.get_job(conn, "nope") is None


# -------------------------------------------------------------- list_jobs --


def test_list_jobs_orders_by_tier_then_score_with_unranked_last(conn):
    a, b, c, d, e = _insert(conn, *[_job(url=f"https://x.example/{n}") for n in range(5)])
    store.set_rule_score(conn, a, 50, "ok")
    store.set_classification(conn, a, tier=1, fit_score=50, category=None, reason="")
    store.set_rule_score(conn, b, 90, "great")
    store.set_classification(conn, b, tier=1, fit_score=90, category=None, reason="")
    store.set_rule_score(conn, c, 99, "great")
    store.set_classification(conn, c, tier=2, fit_score=99, category=None, reason="")
    store.set_rule_score(conn, d, 100, "unclassified")
    # e has neither score nor tier

    assert [j["id"] for j in store.list_jobs(conn)] == [b, a, c, d, e]


def test_list_jobs_breaks_ties_by_newest_scrape(conn):
    older, newer = _insert(conn, _job(url="https://x.example/1"), _job(url="https://x.example/2"))
    conn.execute("UPDATE jobs SET scraped_at = '2024-01-01T00:00:00+00:00' WHERE id = ?", (older,))
    conn.execute("UPDATE jobs SET scraped_at = '2024-01-02T00:00:00+00:00' WHERE id = ?", (newer,))

    assert [j["id"] for j in store.list_jobs(conn)] == [newer, older]


def test_list_jobs_filters(conn):
    a, b, c = _insert(
        conn,
        _job(url="https://x.example/1", source="greenhouse"),
        _job(url="https://x.example/2", source="indeed"),
        _job(url="https://x.example/3", source="indeed"),
    )
    store.set_rule_score(conn, a, 80, "")
    store.set_rule_score(conn, b, 20, "")
    store.set_status(conn, b, "skipped")

    ids = lambda **kw: [j["id"] for j in store.list_jobs(conn, **kw)]  # noqa: E731
    assert ids(status="skipped") == [b]
    assert ids(status="pending") == [a, c]
    assert ids(min_score=50) == [a]
    assert ids(min_score=0) == [a, b], "unscored jobs never pass a score filter"
    assert ids(source="indeed") == [b, c]
    assert ids(source="indeed", status="pending") == [c]
    assert ids(status="applied") == []


def test_list_jobs_paginates(conn):
    ids = _insert(conn, *[_job(url=f"https://x.example/{n}") for n in range(5)])
    for n, jid in enumerate(ids):
        store.set_rule_score(conn, jid, 10 * n, "")

    ranked = [j["id"] for j in store.list_jobs(conn)]
    assert ranked == list(reversed(ids))
    assert [j["id"] for j in store.list_jobs(conn, limit=2)] == ranked[:2]
    assert [j["id"] for j in store.list_jobs(conn, limit=2, offset=2)] == ranked[2:4]
    assert store.list_jobs(conn, limit=2, offset=10) == []


def test_count_jobs_groups_by_status(conn):
    a, b, c = _insert(conn, *[_job(url=f"https://x.example/{n}") for n in range(3)])
    store.set_status(conn, a, "queued")

    assert store.count_jobs(conn) == {"pending": 2, "queued": 1}
    conn.execute("DELETE FROM jobs")
    assert store.count_jobs(conn) == {}


# ------------------------------------------------------------- set_* --


def test_set_status_validates_and_reports_whether_a_row_changed(conn):
    (jid,) = _insert(conn, _job())
    store.set_rule_score(conn, jid, 10, "original reason")

    assert store.set_status(conn, jid, "queued") is True
    assert store.get_job(conn, jid)["status"] == "queued"
    assert store.get_job(conn, jid)["score_reason"] == "original reason"

    assert store.set_status(conn, jid, "skipped", reason="below threshold") is True
    assert store.get_job(conn, jid)["status"] == "skipped"
    assert store.get_job(conn, jid)["score_reason"] == "below threshold"

    assert store.set_status(conn, "missing", "queued") is False


def test_set_status_rejects_unknown_statuses_before_touching_the_db(conn):
    (jid,) = _insert(conn, _job())
    with pytest.raises(ValueError, match="unknown status 'bogus'"):
        store.set_status(conn, jid, "bogus")
    assert store.get_job(conn, jid)["status"] == "pending"


def test_set_description_writes_the_text_and_enriched_at(conn):
    (jid,) = _insert(conn, _job(description="snippet"))
    store.set_description(conn, jid, "the whole posting")

    row = store.get_job(conn, jid)
    assert row["description"] == "the whole posting"
    assert row["enriched_at"] is not None
    assert row["scored_at"] is None and row["classified_at"] is None


def test_set_rule_score_writes_score_reason_and_scored_at(conn):
    (jid,) = _insert(conn, _job())
    store.set_rule_score(conn, jid, 73, "python +20, remote +10")

    row = store.get_job(conn, jid)
    assert row["score"] == 73
    assert row["score_reason"] == "python +20, remote +10"
    assert row["scored_at"] is not None
    assert row["classified_at"] is None


def test_set_classification_writes_tier_fields_and_classified_at(conn):
    (jid,) = _insert(conn, _job())
    store.set_classification(
        conn, jid, tier=2, fit_score=81, category="Backend Engineering", reason="strong match"
    )

    row = store.get_job(conn, jid)
    assert row["tier"] == 2
    assert row["fit_score"] == 81
    assert row["category"] == "Backend Engineering"
    assert row["tier_reason"] == "strong match"
    assert row["classified_at"] is not None
    assert row["scored_at"] is None


def test_set_classification_accepts_a_missing_category(conn):
    (jid,) = _insert(conn, _job())
    store.set_classification(conn, jid, tier=4, fit_score=5, category=None, reason="no")
    assert store.get_job(conn, jid)["category"] is None


# ----------------------------------------------------------- work queues --


def test_jobs_needing_enrichment_picks_missing_and_snippet_descriptions(conn):
    none, short, long, enriched, skipped = _insert(
        conn,
        _job(url="https://x.example/1", description=None),
        _job(url="https://x.example/2", description="short"),
        _job(url="https://x.example/3", description="x" * 400),
        _job(url="https://x.example/4", description="short"),
        _job(url="https://x.example/5", description=None),
    )
    store.set_description(conn, enriched, "still short")
    store.set_status(conn, skipped, "skipped")

    assert {j["id"] for j in store.jobs_needing_enrichment(conn)} == {none, short}


def test_jobs_needing_enrichment_honours_min_chars(conn):
    (jid,) = _insert(conn, _job(description="x" * 399))
    assert [j["id"] for j in store.jobs_needing_enrichment(conn)] == [jid]
    assert store.jobs_needing_enrichment(conn, min_chars=399) == []
    assert [j["id"] for j in store.jobs_needing_enrichment(conn, min_chars=400)] == [jid]


def test_jobs_pending_rules_are_pending_and_unscored(conn):
    fresh, scored, skipped, enriched = _insert(
        conn, *[_job(url=f"https://x.example/{n}") for n in range(4)]
    )
    store.set_rule_score(conn, scored, 50, "")
    store.set_status(conn, skipped, "skipped")
    store.set_description(conn, enriched, "full text")

    assert {j["id"] for j in store.jobs_pending_rules(conn)} == {fresh, enriched}


def test_jobs_pending_classification_selects_by_score_and_orders_best_first(conn):
    low, mid, high, done, unscored, queued = _insert(
        conn, *[_job(url=f"https://x.example/{n}") for n in range(6)]
    )
    store.set_rule_score(conn, low, 59, "")
    store.set_rule_score(conn, mid, 60, "")
    store.set_rule_score(conn, high, 95, "")
    store.set_rule_score(conn, done, 99, "")
    store.set_classification(conn, done, tier=1, fit_score=90, category=None, reason="")
    store.set_rule_score(conn, queued, 98, "")
    store.set_status(conn, queued, "queued")

    assert [j["id"] for j in store.jobs_pending_classification(conn, min_score=60)] == [high, mid]
    assert [j["id"] for j in store.jobs_pending_classification(conn, min_score=0)] == [
        high,
        mid,
        low,
    ]
    assert store.jobs_pending_classification(conn, min_score=96) == []


# ------------------------------------------------------------------- runs --


def test_start_run_creates_an_open_run(conn):
    run_id = store.start_run(conn)
    assert isinstance(run_id, int)

    (run,) = store.list_runs(conn)
    assert run["id"] == run_id
    assert run["started_at"]
    assert run["ended_at"] is None
    assert (run["found"], run["scored"], run["applied"], run["failed"]) == (0, 0, 0, 0)
    assert (run["tokens_in"], run["tokens_out"]) == (0, 0)


def test_finish_run_records_counters_and_the_end_time(conn):
    run_id = store.start_run(conn)
    store.finish_run(conn, run_id, found=12, scored=7, applied=1, failed=2)

    (run,) = store.list_runs(conn)
    assert run["ended_at"] is not None
    assert (run["found"], run["scored"], run["applied"], run["failed"]) == (12, 7, 1, 2)


def test_finish_run_with_no_counters_only_closes_the_run(conn):
    run_id = store.start_run(conn)
    store.add_run_tokens(conn, run_id, 5, 6)
    store.finish_run(conn, run_id)

    (run,) = store.list_runs(conn)
    assert run["ended_at"] is not None
    assert (run["tokens_in"], run["tokens_out"]) == (5, 6)


def test_finish_run_rejects_unknown_counters(conn):
    run_id = store.start_run(conn)
    with pytest.raises(ValueError, match=r"unknown run counters: \['bogus', 'ended_at'\]"):
        store.finish_run(conn, run_id, found=1, bogus=2, ended_at=3)

    (run,) = store.list_runs(conn)
    assert run["ended_at"] is None and run["found"] == 0


def test_add_run_tokens_accumulates(conn):
    run_id = store.start_run(conn)
    store.add_run_tokens(conn, run_id, 100, 20)
    store.add_run_tokens(conn, run_id, 50, 5)

    (run,) = store.list_runs(conn)
    assert (run["tokens_in"], run["tokens_out"]) == (150, 25)


def test_list_runs_is_newest_first_and_limited(conn):
    ids = [store.start_run(conn) for _ in range(3)]
    assert [r["id"] for r in store.list_runs(conn)] == list(reversed(ids))
    assert [r["id"] for r in store.list_runs(conn, limit=2)] == ids[:0:-1]
