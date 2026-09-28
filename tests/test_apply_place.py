"""Where the posting itself says the job is, read on the application site."""

from __future__ import annotations

import json

import pytest

from jobagent.apply.place import PostingPlace, read_place, wrong_place

WANTED = ["remote", "menifee, ca", "temecula, ca", "winchester, ca"]


@pytest.mark.parametrize(
    ("place", "reason"),
    [
        (PostingPlace(["Lonoke, AR"], None), "Lonoke, AR"),
        (PostingPlace(["Lonoke, AR"], False), "Lonoke, AR"),
        (PostingPlace(["Hybrid - Austin, TX"], False), "Austin, TX"),
        (PostingPlace(["Lonoke, AR"], True), None),
        (PostingPlace(["Temecula, CA"], None), None),
        (PostingPlace(["Temecula, California"], False), None),
        (PostingPlace(["US - Remote"], None), None),
        (PostingPlace(["United States"], None), None),
        (PostingPlace(["3 Locations"], None), None),
        (PostingPlace([], None), None),
        (PostingPlace(["Lonoke, AR", "Menifee, CA"], None), None),
        (PostingPlace(["Lonoke, AR", "United States"], None), None),
    ],
)
def test_only_a_specific_place_outside_the_wanted_ones_is_ruled_out(place, reason):
    found = wrong_place(place, WANTED)
    if reason is None:
        assert found is None
    else:
        assert found is not None and reason in found and "not remote" in found


def test_with_no_places_wanted_nothing_is_ruled_out():
    assert wrong_place(PostingPlace(["Lonoke, AR"], False), []) is None
    only_remote = wrong_place(PostingPlace(["Lonoke, AR"], None), ["Remote"])
    assert only_remote and "only Remote is wanted" in only_remote


def _posting(page, body: str, ld: dict | None = None) -> None:
    script = (
        f'<script type="application/ld+json">{json.dumps(ld)}</script>' if ld is not None else ""
    )
    page.set_content(f"<html><head>{script}</head><body>{body}</body></html>")


def test_the_schema_org_posting_is_read(page):
    ld = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "jobLocation": {
            "@type": "Place",
            "address": {"addressLocality": "Lonoke", "addressRegion": "AR"},
        },
    }
    _posting(page, "<h1>Sr. Network Engineer</h1>", ld)
    assert read_place(page) == PostingPlace(["Lonoke, AR"], None)

    ld["jobLocationType"] = "TELECOMMUTE"
    _posting(page, "<h1>Sr. Network Engineer</h1>", ld)
    assert read_place(page).remote is True

    graph = {
        "@context": "https://schema.org",
        "@graph": [{"@type": "Organization"}, dict(ld, jobLocationType=None)],
    }
    _posting(page, "<p>Report to the plant.</p>", graph)
    assert read_place(page) == PostingPlace(["Lonoke, AR"], None)


def test_the_sites_own_fields_and_words_are_read(page):
    fields = (
        '<div data-automation-id="locations"><dl><dt>locations</dt><dd>Lonoke, AR</dd></dl></div>'
        '<div data-automation-id="remoteType"><dl><dt>remote type</dt><dd>{}</dd></dl></div>'
    )
    selectors = ('[data-automation-id="locations"] dd',)
    remote = ('[data-automation-id="remoteType"] dd',)
    _posting(page, fields.format("Hybrid"))
    assert read_place(page, selectors, remote) == PostingPlace(["Lonoke, AR"], False)
    _posting(page, fields.format("Fully Remote"))
    assert read_place(page, selectors, remote).remote is True
    _posting(page, fields.format("") + "<p>This is a fully remote position.</p>")
    assert read_place(page, selectors, remote).remote is True
    _posting(
        page,
        fields.format("")
        + "<p>This is not a remote position; fully remote staff apply elsewhere.</p>",
    )
    assert read_place(page, selectors, remote).remote is None
    _posting(page, "<p>Nothing about where.</p>")
    assert read_place(page, selectors, remote) == PostingPlace([], None)
