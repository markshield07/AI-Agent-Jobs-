"""Dice's own search: the URL it asks for, the cards it reads, and the browser
fallback when the page comes without its results. No network: the HTTP
client is a MockTransport and the browser a stub."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx

from jobagent.discovery.criteria import SearchCriteria
from jobagent.discovery.sources import all_sources
from jobagent.discovery.sources.dice import DiceSource, parse_results, search_url

GUID_A = "0b6a2f52-4c1e-4f0e-9d7c-2a1b3c4d5e6f"
GUID_B = "9f8e7d6c-5b4a-4321-8765-0fedcba98765"
GUID_C = "1a2b3c4d-1111-2222-3333-444455556666"


def card(guid, title, company, place, employment="Full-time"):
    return f"""
    <div data-testid="job-card">
      <a data-testid="job-search-job-card-link" href="https://www.dice.com/job-detail/{guid}"
         aria-label="View Details for {title} ({"a" * 32})"></a>
      <span data-testid="job-search-job-detail-link" role="link">{title}</span>
      <p data-testid="job-card-company-name">{company}</p>
      <p class="text-sm font-normal text-zinc-600">{place}</p>
      <p id="employmentType-label">{employment}</p>
    </div>"""


RESULTS = (
    "<html><body><header><a href='/jobs'>Jobs</a></header><main>"
    + card(GUID_A, "Senior Network Engineer", "Acme Networks", "Remote")
    + card(GUID_B, "Network Engineer III", "Globex", "Remote", "Contract - W2")
    + card(GUID_C, "Java Developer", "Initech", "Remote")
    + "</main></body></html>"
)
EMPTY = "<html><body><div id='__next'></div></body></html>"


def criteria(**kw) -> SearchCriteria:
    base = {"titles": ["Network Engineer"], "locations": ["Remote"], "max_age_hours": 72}
    return SearchCriteria(**{**base, **kw})


def client_returning(html: str, seen: list[str]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text=html)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_the_search_asks_for_easy_apply_full_time_and_the_age_wanted():
    remote = parse_qs(urlparse(search_url("Network Engineer", "Remote", criteria())).query)
    assert remote["q"] == ["Network Engineer"]
    assert remote["filters.easyApply"] == ["true"]
    assert remote["filters.employmentType"] == ["FULLTIME"]
    assert remote["filters.postedDate"] == ["THREE"]
    assert remote["filters.workplaceTypes"] == ["Remote"]
    assert "location" not in remote

    near = parse_qs(
        urlparse(search_url("Network Engineer", "Riverside, CA", criteria(max_age_hours=24))).query
    )
    assert near["location"] == ["Riverside, CA"] and near["filters.radius"] == ["30"]
    assert near["filters.postedDate"] == ["ONE"]
    assert "filters.workplaceTypes" not in near


def test_cards_become_jobs_that_apply_on_dice():
    seen: list[str] = []
    source = DiceSource(client_returning(RESULTS, seen), render=lambda url: "")
    jobs = list(source.search(criteria()))

    assert len(seen) == 1 and "filters.easyApply=true" in seen[0]
    assert [j.title for j in jobs] == ["Senior Network Engineer"], (
        "the contract role and the title that does not match are left out"
    )
    job = jobs[0]
    assert job.url == f"https://www.dice.com/job-detail/{GUID_A}"
    assert job.apply_url == job.url and job.ats_type == "dice" and job.applies_on_board
    assert job.company == "Acme Networks" and job.source == "dice"
    assert job.remote is True and job.external_id == GUID_A


def test_a_place_search_records_where_it_searched():
    source = DiceSource(client_returning(RESULTS, []), render=lambda url: "")
    jobs = list(source.search(criteria(locations=["Riverside, CA"])))
    assert jobs and jobs[0].found_near == "Riverside, CA"


def test_a_page_without_its_results_is_read_in_a_browser():
    rendered: list[str] = []

    def render(url: str) -> str:
        rendered.append(url)
        return RESULTS

    source = DiceSource(client_returning(EMPTY, []), render=render)
    jobs = list(source.search(criteria()))
    assert len(rendered) == 1 and [j.title for j in jobs] == ["Senior Network Engineer"]


def test_a_refused_page_is_read_in_a_browser_and_a_failed_one_skipped():
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="no")

    client = httpx.Client(transport=httpx.MockTransport(refuse))
    assert [j.title for j in DiceSource(client, render=lambda url: RESULTS).search(criteria())]

    def broken(url: str) -> str:
        raise RuntimeError("no browser")

    assert list(DiceSource(client, render=broken).search(criteria())) == []


def test_bare_posting_links_are_read_when_there_are_no_cards():
    html = (
        f"<a href='/job-detail/{GUID_A}?searchlink=x'>Senior Network Engineer</a>"
        f"<a href='/job-detail/{GUID_A}'>Senior Network Engineer</a>"
        "<a href='/company-profile/acme'>Acme</a>"
    )
    assert parse_results(html) == [
        {
            "id": GUID_A,
            "title": "Senior Network Engineer",
            "company": "",
            "location": "",
            "employment": "",
        }
    ]


def test_dice_is_searched_only_when_applications_may_go_there():
    c = criteria(jobspy_sites=["linkedin"])
    assert "dice" not in [s.name for s in all_sources(c, apply_sites=("linkedin",))]
    names = [s.name for s in all_sources(c, apply_sites=("linkedin", "dice"))]
    assert names == ["jobspy", "dice"]
    jobspy = all_sources(c, apply_sites=("linkedin", "dice"))[0]
    assert jobspy.on_site_only, "LinkedIn is still asked for Easy Apply jobs only"
