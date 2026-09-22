"""Filing a reply against the right application.

The cases that matter are the ones where the matcher should refuse: a
newsletter, a mail that arrived before the application was sent, and a company
name that appears with nothing else to back it up. Filing those wrongly would
put a rejection on a job the user is still waiting to hear about.
"""

from __future__ import annotations

import pytest

from jobagent.apply import store as applications
from jobagent.discovery import store as jobs
from jobagent.discovery.models import RawJob
from jobagent.inbox.match import (
    THRESHOLD,
    Candidate,
    candidates,
    company_matches_domain,
    company_tokens,
    domain_root,
    is_ats_domain,
    match_message,
    score,
)
from jobagent.inbox.models import InboxMessage

APPLIED_AT = "2026-09-20T12:00:00+00:00"
LATER = "2026-09-21T09:00:00+00:00"


def message(**kwargs) -> InboxMessage:
    base = {
        "message_id": "m1",
        "subject": "",
        "from_addr": "someone@example.com",
        "from_name": "",
        "body": "",
        "received_at": LATER,
    }
    return InboxMessage(**{**base, **kwargs})


def candidate(**kwargs) -> Candidate:
    base = {
        "application_id": 1,
        "company": "Acme Robotics",
        "title": "Senior Backend Engineer",
        "job": {"url": "https://boards.greenhouse.io/acme/jobs/1", "apply_url": None},
        "since": APPLIED_AT,
    }
    return Candidate(**{**base, **kwargs})


# ------------------------------------------------------------- the pieces --


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("acmerobotics.com", "acmerobotics.com"),
        ("careers.mail.acmerobotics.com", "acmerobotics.com"),
        ("us.greenhouse-mail.io", "greenhouse-mail.io"),
        ("jobs.acme.co.uk", "acme.co.uk"),
        ("", ""),
    ],
)
def test_domain_root_keeps_the_part_that_identifies_the_sender(given, expected):
    assert domain_root(given) == expected


@pytest.mark.parametrize(
    "domain",
    ["no-reply@us.greenhouse-mail.io".split("@")[1], "jobs.lever.co", "notify.ashbyhq.com"],
)
def test_an_ats_domain_is_recognised_through_its_subdomains(domain):
    assert is_ats_domain(domain)


def test_a_company_domain_is_not_an_ats_domain():
    assert not is_ats_domain("acmerobotics.com")


def test_the_legal_suffix_is_not_part_of_the_company_name():
    assert company_tokens("Acme Robotics, Inc.") == ["acme", "robotics"]
    assert company_tokens("Stripe") == ["stripe"]


@pytest.mark.parametrize(
    ("company", "domain", "matches"),
    [
        ("Acme Robotics", "acmerobotics.com", True),
        ("Acme Robotics, Inc.", "careers.acmerobotics.com", True),
        ("Stripe", "stripe.com", True),
        ("Acme Robotics", "acme-robotics.com", True),
        ("Acme Robotics", "totallyunrelated.com", False),
        ("Acme", "acmecorp.example", False),
    ],
)
def test_a_company_name_is_compared_with_a_domain(company, domain, matches):
    assert company_matches_domain(company, domain) is matches


# -------------------------------------------------------------- the score --


def test_a_mail_from_the_company_domain_is_enough_on_its_own():
    total, reasons = score(message(from_addr="dana@acmerobotics.com"), candidate())
    assert total >= THRESHOLD
    assert "sender domain matches the company" in reasons


def test_the_jobs_own_domain_counts_even_when_the_name_does_not():
    spot = candidate(company="Acme Robotics", job={"url": "https://careers.acme-hq.io/1"})
    total, reasons = score(message(from_addr="dana@acme-hq.io"), spot)
    assert total >= THRESHOLD and "sender domain is the company's" in reasons


def test_an_ats_relay_needs_the_company_and_the_role_in_the_message():
    from_ats = message(
        from_addr="no-reply@us.greenhouse-mail.io",
        from_name="Acme Robotics",
        subject="Senior Backend Engineer",
    )
    total, _ = score(from_ats, candidate())
    assert total >= THRESHOLD


def test_an_ats_relay_saying_nothing_identifying_does_not_match():
    bare = message(from_addr="no-reply@us.greenhouse-mail.io", subject="An update")
    assert score(bare, candidate())[0] < THRESHOLD


def test_a_company_name_alone_is_not_enough():
    total, _ = score(message(subject="Acme Robotics is hiring"), candidate())
    assert total < THRESHOLD


def test_a_mail_that_arrived_before_we_applied_scores_nothing():
    early = message(from_addr="dana@acmerobotics.com", received_at="2026-09-19T08:00:00+00:00")
    assert score(early, candidate()) == (0.0, [])


def test_the_title_counts_in_the_subject_and_less_in_the_body():
    in_subject = message(subject="Senior Backend Engineer at Acme Robotics")
    in_body = message(body="about the Senior Backend Engineer role")
    assert score(in_subject, candidate())[0] > score(in_body, candidate())[0]


# ---------------------------------------------------------------- picking --


def test_a_newsletter_matches_nothing():
    newsletter = message(
        from_addr="digest@jobsweekly.example",
        from_name="Jobs Weekly",
        subject="12 new backend roles this week",
    )
    assert match_message(newsletter, [candidate()]) is None


def test_the_better_scoring_application_wins():
    pool = [
        candidate(application_id=1, title="Staff Platform Engineer"),
        candidate(application_id=2, title="Senior Backend Engineer"),
    ]
    found = match_message(
        message(from_addr="dana@acmerobotics.com", subject="Senior Backend Engineer"), pool
    )
    assert found is not None and found.application_id == 2


def test_a_match_carries_why_it_matched():
    found = match_message(
        message(from_addr="dana@acmerobotics.com", subject="Senior Backend Engineer"),
        [candidate()],
    )
    assert found is not None
    assert "sender domain" in found.reason and "title in subject" in found.reason
    assert 0 < found.confidence <= 1.0


# ------------------------------------------------------------- candidates --


def _application(conn, *, company: str, title: str, url: str, submitted: str | None) -> int:
    raw = RawJob(url=url, title=title, company=company, source="greenhouse", ats_type="greenhouse")
    job_id = jobs.upsert_jobs(conn, [raw]).new_ids[0]
    app_id = applications.get_or_create_application(conn, job_id, mode="auto", ats="greenhouse")
    if submitted:
        conn.execute("UPDATE applications SET submitted_at = ? WHERE id = ?", (submitted, app_id))
    return app_id


def test_candidates_reads_applications_newest_first(conn):
    older = _application(
        conn,
        company="Acme Robotics",
        title="Senior Backend Engineer",
        url="https://boards.greenhouse.io/acme/jobs/1",
        submitted="2026-09-18T10:00:00+00:00",
    )
    newer = _application(
        conn,
        company="Globex",
        title="Platform Engineer",
        url="https://boards.greenhouse.io/globex/jobs/2",
        submitted="2026-09-21T10:00:00+00:00",
    )
    found = candidates(conn)
    assert [c.application_id for c in found] == [newer, older]
    assert found[1].company == "Acme Robotics"
    assert found[1].since == "2026-09-18T10:00:00+00:00"


def test_candidates_can_leave_out_applications_older_than_a_date(conn):
    _application(
        conn,
        company="Acme Robotics",
        title="Senior Backend Engineer",
        url="https://boards.greenhouse.io/acme/jobs/1",
        submitted="2026-08-01T10:00:00+00:00",
    )
    kept = _application(
        conn,
        company="Globex",
        title="Platform Engineer",
        url="https://boards.greenhouse.io/globex/jobs/2",
        submitted="2026-09-21T10:00:00+00:00",
    )
    assert [c.application_id for c in candidates(conn, since="2026-09-01")] == [kept]


def test_an_application_that_was_never_submitted_still_counts(conn):
    """A form filled in review mode can still draw a reply once the user sends it."""
    app_id = _application(
        conn,
        company="Acme Robotics",
        title="Senior Backend Engineer",
        url="https://boards.greenhouse.io/acme/jobs/1",
        submitted=None,
    )
    found = candidates(conn)
    assert [c.application_id for c in found] == [app_id]
    assert found[0].since  # created_at stands in for submitted_at
