from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx

from jobagent.config import Settings, board_apply_only
from jobagent.discovery.criteria import BoardRef, SearchCriteria
from jobagent.discovery.scoring.rules import remote_only_reason
from jobagent.discovery.sources import all_sources
from jobagent.discovery.sources.company_boards import CATALOG, with_company_boards
from jobagent.discovery.sources.greenhouse import _to_raw
from jobagent.discovery.sources.remote_boards import (
    HimalayasSource,
    WeWorkRemotelySource,
    WorkingNomadsSource,
    resolve_apply_url,
    us_friendly,
)

CRITERIA = SearchCriteria(titles=["Network Engineer", "IT Operations Manager"])
GH = "https://job-boards.greenhouse.io/acme/jobs/123"
LEVER = "https://jobs.lever.co/acme/abc-123"
LONG = "<p>" + "Run the network. " * 40 + "</p>"


def client(routes: dict[str, Any]) -> httpx.Client:
    """Routes by host + path. A value that is a str starting with 'redirect:'
    answers 302 to the rest; another str is HTML/XML; anything else JSON."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = routes.get(f"{request.url.host}{request.url.path}")
        if body is None:
            return httpx.Response(404, text="not found")
        if isinstance(body, str) and body.startswith("redirect:"):
            return httpx.Response(302, headers={"Location": body[len("redirect:") :]})
        if isinstance(body, str):
            return httpx.Response(200, text=body)
        return httpx.Response(200, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def rss(*items: dict[str, str]) -> str:
    def item(fields: dict[str, str]) -> str:
        return (
            "<item>" + "".join(f"<{k}><![CDATA[{v}]]></{k}>" for k, v in fields.items()) + "</item>"
        )

    return (
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        + "".join(map(item, items))
        + "</channel></rss>"
    )


def now_rfc822(hours_ago: float = 1) -> str:
    return format_datetime(datetime.now(UTC) - timedelta(hours=hours_ago))


def test_weworkremotely_keeps_matching_us_jobs_that_apply_on_greenhouse_or_lever():
    feed = rss(
        {
            "title": "Acme: Senior Network Engineer",
            "link": "https://weworkremotely.com/remote-jobs/acme-senior-network-engineer",
            "region": "USA Only",
            "pubDate": now_rfc822(),
            "description": LONG,
        },
        {
            "title": "Beta: Network Engineer",
            "link": "https://weworkremotely.com/remote-jobs/beta-network-engineer",
            "region": "Europe Only",
            "pubDate": now_rfc822(),
            "description": LONG,
        },
        {
            "title": "Gamma: Network Engineer",
            "link": "https://weworkremotely.com/remote-jobs/gamma-network-engineer",
            "region": "Anywhere in the World",
            "pubDate": now_rfc822(),
            "description": LONG,
        },
        {
            "title": "Delta: Sales Manager",
            "link": "https://weworkremotely.com/remote-jobs/delta-sales",
            "region": "USA Only",
            "pubDate": now_rfc822(),
            "description": LONG,
        },
    )
    routes = {
        "weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss": feed,
        "weworkremotely.com/remote-jobs/acme-senior-network-engineer": f'<a href="{GH}">Apply</a>',
        # Applies on the company's own page: not a form the agent fills.
        "weworkremotely.com/remote-jobs/gamma-network-engineer": '<a href="https://gamma.example/careers">Apply</a>',
    }
    jobs = list(WeWorkRemotelySource(client(routes)).search(CRITERIA))
    assert [(j.company, j.title) for j in jobs] == [("Acme", "Senior Network Engineer")]
    job = jobs[0]
    assert job.source == "weworkremotely" and job.remote is True
    assert job.url.startswith("https://weworkremotely.com/"), "the listing is kept as the link back"
    assert job.apply_url == GH and job.ats_type == "greenhouse"


def test_workingnomads_follows_its_go_link_to_the_form():
    listings = [
        {
            "url": "https://www.workingnomads.com/job/go/1/",
            "title": "Network Engineer (100% remote)",
            "company_name": "Acme",
            "location": "USA",
            "description": LONG,
            "pub_date": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
        },
        {
            "url": "https://www.workingnomads.com/job/go/2/",
            "title": "Network Engineer",
            "company_name": "Old",
            "location": "USA",
            "description": LONG,
            "pub_date": (datetime.now(UTC) - timedelta(days=30)).isoformat(),
        },
    ]
    routes = {
        "www.workingnomads.com/api/exposed_jobs/": listings,
        "www.workingnomads.com/job/go/1/": f"redirect:{LEVER}",
        "jobs.lever.co/acme/abc-123": "<html>form</html>",
    }
    jobs = list(WorkingNomadsSource(client(routes)).search(CRITERIA))
    assert len(jobs) == 1, "the month-old listing is past max_age_hours"
    assert jobs[0].apply_url == LEVER and jobs[0].ats_type == "lever"


def test_himalayas_searches_each_title_once_and_reads_annual_pay():
    seen: list[str] = []
    entry = {
        "title": "IT Operations Manager",
        "companyName": "Acme",
        "guid": "https://himalayas.app/companies/acme/jobs/it-ops",
        "applicationLink": GH,
        "locationRestrictions": ["United States"],
        "description": LONG,
        "pubDate": int((datetime.now(UTC) - timedelta(hours=3)).timestamp()),
        "minSalary": 150000,
        "maxSalary": 180000,
        "salaryPeriod": "annual",
        "currency": "USD",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("q"))
        assert request.url.params.get("country") == "US"
        return httpx.Response(200, json={"jobs": [entry]})

    source = HimalayasSource(httpx.Client(transport=httpx.MockTransport(handler)))
    jobs = list(source.search(CRITERIA))
    assert seen == ["Network Engineer", "IT Operations Manager"]
    assert len(jobs) == 1, "the same guid from two searches is one job"
    assert (jobs[0].salary_min, jobs[0].salary_max) == (150000, 180000)
    assert jobs[0].posted_at and jobs[0].posted_at.startswith(str(datetime.now(UTC).year)[:3])


def test_resolve_apply_url_without_a_supported_form_is_none():
    routes = {"x.example/job": '<a href="https://acme.wd5.myworkdayjobs.com/x">Apply</a>'}
    assert resolve_apply_url(client(routes), "https://x.example/job") is None
    assert resolve_apply_url(client({}), GH) == GH, "an ATS link is used without a request"
    assert resolve_apply_url(client({}), "https://x.example/missing") is None


def test_us_friendly():
    assert us_friendly(None) and us_friendly("Remote") and us_friendly("USA Only")
    assert us_friendly("Anywhere in the World") and us_friendly("North America")
    assert not us_friendly("Europe Only") and not us_friendly("Canada")


def test_a_remote_board_listing_counts_as_saying_fully_remote_but_office_words_still_win():
    job = {"title": "Network Engineer", "location": "USA Only", "description": "Run things. " * 50}
    assert remote_only_reason({**job, "source": "linkedin"}) is not None
    assert remote_only_reason({**job, "source": "weworkremotely"}) is None
    hybrid = {
        **job,
        "description": job["description"] + " This is a hybrid role, 3 days in office.",
    }
    assert remote_only_reason({**hybrid, "source": "himalayas"}) is not None


def test_all_sources_adds_remote_boards_only_with_titles():
    names = [s.name for s in all_sources(CRITERIA, remote_boards=("himalayas", "weworkremotely"))]
    assert names[-2:] == ["himalayas", "weworkremotely"]
    assert not [
        s
        for s in all_sources(SearchCriteria(), remote_boards=("himalayas",))
        if s.name == "himalayas"
    ]


def test_company_boards_follow_the_apply_sites():
    criteria = SearchCriteria(boards=[BoardRef(ats="greenhouse", slug="cloudflare", company="CF")])
    both = with_company_boards(criteria, ("linkedin", "greenhouse", "lever"))
    assert {b.ats for b in both.boards} == {"greenhouse", "lever"}
    assert [b.company for b in both.boards if b.slug == "cloudflare"] == ["CF"], "criteria win"
    assert len(both.boards) == len(CATALOG)
    only_gh = with_company_boards(criteria, ("greenhouse",))
    assert {b.ats for b in only_gh.boards} == {"greenhouse"}
    assert with_company_boards(criteria, ("linkedin", "dice")) is criteria


def test_naming_greenhouse_and_lever_keeps_linkedin_on_easy_apply():
    assert board_apply_only(("linkedin", "dice"))
    assert board_apply_only(("linkedin", "dice", "greenhouse", "lever"))
    assert not board_apply_only(("greenhouse",))
    assert not board_apply_only(("linkedin", "workday"))
    assert not board_apply_only(())
    settings = Settings(
        apply_sites="linkedin,greenhouse", remote_boards="Himalayas, weworkremotely"
    )
    assert settings.board_apply_only
    assert settings.remote_board_list == ("himalayas", "weworkremotely")


def test_greenhouse_on_a_company_careers_page_applies_on_greenhouse_hosted_page():
    entry = {
        "id": 77,
        "title": "Network Engineer",
        "absolute_url": "https://acme.com/careers?gh_jid=77",
    }
    job = _to_raw(entry, "Acme", "acme")
    assert job.url == "https://acme.com/careers?gh_jid=77"
    assert job.apply_url == "https://job-boards.greenhouse.io/acme/jobs/77"
    hosted = _to_raw({**entry, "absolute_url": GH}, "Acme", "acme")
    assert hosted.apply_url == GH


def test_remote_boards_are_read_at_most_every_few_hours(conn):
    from jobagent.discovery.pipeline import RunReport, _due_sources

    board = HimalayasSource(client({}))
    other = WorkingNomadsSource(client({}))
    other.min_interval_hours = 0  # read every run
    first = _due_sources(conn, [board, other], RunReport(run_id=1))
    assert first == [board, other]
    second = _due_sources(conn, [board, other], RunReport(run_id=2))
    assert second == [other], "himalayas was read moments ago"
