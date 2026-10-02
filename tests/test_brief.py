"""A posting in brief: summary, duties and asks, and the pay it states."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jobagent.discovery import brief
from jobagent.discovery import store as jobs
from jobagent.discovery.models import RawJob
from jobagent.main import create_app

ROLE = (
    "We're looking for a Network Operations Manager to lead our NOC across three data centers."
    " You will own incident response."
)
MARKDOWN = r"""**About Northwind**

Northwind is a leader in networking gear\.

**About the Role**

ROLE_HERE

**Responsibilities**

* Lead a team of 8 engineers
* Own the Cisco routing estate \(BGP, OSPF\)
* Run [change management](https://example.com/cm)

**Qualifications:**

- 5\+ years managing network operations
- Experience with UPS, generators and CRAC systems

The base salary range for this role is $135,000 \- $165,000 plus bonus and equity.

We are an equal opportunity employer.
""".replace("ROLE_HERE", ROLE)

# Company boards: one line per paragraph or list item, no markup.
PLAIN = """Overview
Acme is on a mission to stop outages.
About the Role:
The Senior Network Engineer designs and runs the global backbone with the SRE team.
What You'll Do:
Design the WAN
Run BGP
What You'll Need:
10 years in networking
Benefits
401k match
The annual salary range is $150K–$190K USD."""


def test_the_summary_is_the_postings_words_about_the_role():
    assert brief.summary(MARKDOWN) == ROLE
    # "About the Role" wins over a generic "Overview" about the company.
    assert brief.summary(PLAIN).startswith("The Senior Network Engineer designs")
    assert brief.summary(None) == "" and brief.summary("") == ""
    # With no heading to go by, pay and legal lines are passed over.
    no_heads = (
        "The base salary range for this role is $135,000 - $165,000 plus bonus.\n"
        "We are an equal opportunity employer and value a diverse workforce.\n"
        "You will lead our network team and own every outage from start to finish."
    )
    assert brief.summary(no_heads).startswith("You will lead our network team")


def test_a_long_summary_is_cut_at_a_sentence_or_a_word():
    text = "About the Role\n" + " ".join(["The work is steady and varied."] * 30)
    cut = brief.summary(text, limit=100)
    assert len(cut) <= 100 and cut.endswith(".")
    assert brief.summary("x " * 300, limit=50).endswith("…")


def test_duties_and_asks_come_from_their_sections():
    found = brief.highlights(MARKDOWN)
    assert found["duties"] == [
        "Lead a team of 8 engineers",
        "Own the Cisco routing estate (BGP, OSPF)",
        "Run change management",
    ]
    # A list item that starts like a heading is still a list item.
    assert found["needs"] == [
        "5+ years managing network operations",
        "Experience with UPS, generators and CRAC systems",
    ]
    assert brief.highlights(PLAIN) == {
        "duties": ["Design the WAN", "Run BGP"],
        "needs": ["10 years in networking"],
    }
    assert brief.highlights("Just one line.") == {"duties": [], "needs": []}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Salary: $120,000 - $150,000 per year", (120000, 150000)),
        ("Pay range $95k to $120k", (95000, 120000)),
        ("between $100000 and $130000", (100000, 130000)),
        ("Zone 1: $150,000 - $180,000. Zone 2: $135,000 - $162,000.", (135000, 180000)),
        ("$135,000 \\- $165,000 plus bonus", (135000, 165000)),
        ("$45 - $55 per hour", None),
        ("$60,000 - $70,000/hr", None),
        ("A $10,000 - $25,000 signing bonus", None),
        ("We raised $50,000,000 - $80,000,000 in funding.", None),
        ("Eligible for bonus. The range is $120,000 - $140,000.", (120000, 140000)),
        ("Salary $120,000", None),
        ("$150,000 - $120,000", None),
        ("No pay stated.", None),
    ],
)
def test_pay_is_read_only_from_a_yearly_range_in_the_text(text, expected):
    assert brief.pay_in_text(text) == expected


def test_pay_prefers_the_sites_fields_and_never_guesses():
    assert brief.pay({"salary_min": 100000, "salary_max": 120000, "description": PLAIN}) == {
        "min": 100000,
        "max": 120000,
        "period": "year",
        "from": "listing",
    }
    assert brief.pay({"description": PLAIN})["from"] == "description"
    assert brief.pay({"description": "Great team, great perks."}) is None
    assert brief.pay({"salary_min": 0, "salary_max": None}) is None


def test_a_card_drops_the_full_text():
    card = brief.card({"id": "j", "title": "T", "description": MARKDOWN, "salary_min": None})
    assert "description" not in card
    assert card["summary"].startswith("We're looking") and card["pay"]["min"] == 135000


# ------------------------------------------------------------------- api --


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _job(conn, n, **fields):
    raw = RawJob(
        url=f"https://example.com/{n}",
        title=f"Job {n}",
        company=f"Co {n}",
        source="lever",
        **fields,
    )
    return jobs.upsert_jobs(conn, [raw]).new_ids[0]


def test_lists_carry_the_brief_and_details_the_full_text(client, conn):
    a = _job(conn, 1, description=MARKDOWN)
    b = _job(conn, 2, description=PLAIN, salary_min=140000, salary_max=160000)
    jobs.set_status(conn, a, "queued")
    jobs.set_status(conn, b, "queued")
    conn.execute(
        "INSERT INTO resume_variants (job_id, facts_used, status, created_at)"
        " VALUES (?, '[]', 'ready', '2026-09-28T00:00:00+00:00')",
        (b,),
    )
    conn.commit()

    listed = {j["id"]: j for j in client.get("/api/jobs").json()}
    assert "description" not in listed[a]
    assert listed[a]["pay"]["from"] == "description" and listed[b]["pay"]["from"] == "listing"
    assert listed[a]["resume_ready"] is False and listed[b]["resume_ready"] is True

    one = client.get(f"/api/jobs/{a}").json()
    assert one["description"] == MARKDOWN
    assert one["highlights"]["duties"][0] == "Lead a team of 8 engineers"
    assert one["summary"].startswith("We're looking")


def test_the_overview_shows_the_latest_sent_and_the_queue(client, conn):
    ids = [_job(conn, n, description=PLAIN) for n in range(4)]
    for jid, sent in zip(ids[:3], ("2026-09-26", "2026-09-28", None), strict=True):
        conn.execute(
            "INSERT INTO applications (job_id, mode, ats, submitted_at, created_at)"
            " VALUES (?, 'auto', 'lever', ?, '2026-09-25T00:00:00+00:00')",
            (jid, f"{sent}T10:00:00+00:00" if sent else None),
        )
    jobs.set_status(conn, ids[3], "queued")
    conn.commit()

    out = client.get("/api/overview", params={"limit": 5}).json()
    assert [a["job"]["id"] for a in out["recent"]] == [ids[1], ids[0]], "sent only, newest first"
    assert out["recent"][0]["job"]["summary"].startswith("The Senior Network Engineer")
    assert "description" not in out["recent"][0]["job"]
    assert [j["id"] for j in out["up_next"]] == [ids[3]] and out["queued"] == 1

    detail = client.get(f"/api/applications/{out['recent'][0]['id']}").json()
    assert detail["job"]["description"] == PLAIN
    assert detail["job"]["highlights"]["needs"] == ["10 years in networking"]


def test_the_queue_ahead_is_only_the_sites_applied_on(settings, conn):
    linkedin = RawJob(
        url="https://www.linkedin.com/jobs/view/1", title="Job L", company="Co L", source="linkedin"
    )
    indeed = RawJob(
        url="https://www.indeed.com/viewjob?jk=1", title="Job I", company="Co I", source="indeed"
    )
    on_board, off = jobs.upsert_jobs(conn, [linkedin, indeed]).new_ids
    for jid in (on_board, off):
        jobs.set_status(conn, jid, "queued")
    conn.commit()

    linkedin_only = settings.model_copy(update={"apply_sites": "linkedin"})
    with TestClient(create_app(linkedin_only)) as client:
        out = client.get("/api/overview").json()
    assert [j["id"] for j in out["up_next"]] == [on_board] and out["queued"] == 1
