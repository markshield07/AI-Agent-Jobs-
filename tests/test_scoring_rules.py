from __future__ import annotations

from typing import Any

import pytest

from jobagent.discovery.criteria import SearchCriteria
from jobagent.discovery.scoring.rules import RuleScore, passes, score_rules


def crit(**overrides: Any) -> SearchCriteria:
    """Criteria with nothing constrained unless a test says so."""
    base: dict[str, Any] = {"titles": [], "locations": [], "keywords": []}
    base.update(overrides)
    return SearchCriteria(**base)


def job(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Software Engineer",
        "company": "Acme",
        "location": "Austin, TX",
        "description": "",
        "salary_min": None,
        "salary_max": None,
        "remote": None,
    }
    base.update(fields)
    return base


def score(criteria: SearchCriteria, tags: set[str] | None = None, **fields: Any) -> RuleScore:
    return score_rules(job(**fields), criteria, tags or set())


def rejected(criteria: SearchCriteria, **fields: Any) -> bool:
    return score(criteria, **fields).disqualified


def points(component: str, criteria: SearchCriteria, **fields: Any) -> int:
    return score(criteria, **fields).breakdown[component]


# ----------------------------------------------------------- disqualifiers --


def test_excluded_keyword_in_description_disqualifies():
    result = score(
        crit(exclude_keywords=["clearance"]),
        description="Must hold an active security clearance.",
    )
    assert result.disqualified is True
    assert result.score == 0
    assert result.breakdown == {}
    assert result.reason == "excluded keyword: clearance"


def test_excluded_keyword_in_title_disqualifies():
    result = score(crit(exclude_keywords=["Sales"]), title="Sales Engineer")
    assert result.disqualified is True
    assert result.reason == "excluded keyword: Sales"


def test_excluded_single_word_is_whole_word_only():
    criteria = crit(exclude_keywords=["go"])
    assert rejected(criteria, description="We use Google Cloud.") is False
    assert rejected(criteria, description="Services are written in Go.") is True
    assert rejected(crit(exclude_keywords=["clearance"]), description="clearances desk") is False


def test_excluded_phrase_is_substring_and_whitespace_tolerant():
    criteria = crit(exclude_keywords=["security clearance"])
    assert rejected(criteria, description="Requires a Security  Clearance.") is True
    assert rejected(criteria, description="security\nclearance needed") is True
    assert rejected(criteria, description="security and clearance") is False


def test_blacklisted_company_matches_either_direction():
    result = score(crit(company_blacklist=["acme"]), company="Acme Corp")
    assert result.reason == "blacklisted company: acme"
    assert rejected(crit(company_blacklist=["Acme Corporation Inc"]), company="Acme") is True
    assert rejected(crit(company_blacklist=["Evil Inc"]), company="Acme") is False


def test_blacklist_never_matches_a_missing_company():
    assert rejected(crit(company_blacklist=["Acme"]), company=None) is False
    assert rejected(crit(company_blacklist=["Acme"]), company="  ") is False


def test_salary_below_minimum_disqualifies():
    result = score(crit(salary_min=120000), salary_min=70000, salary_max=90000)
    assert result.disqualified is True
    assert result.score == 0
    assert result.reason == "salary 90000 below minimum 120000"


def test_salary_disqualifier_needs_both_sides_known():
    assert rejected(crit(salary_min=120000), salary_max=None) is False
    assert rejected(crit(salary_min=None), salary_max=90000) is False
    assert rejected(crit(salary_min=120000), salary_min=50000, salary_max=None) is False


def test_disqualifiers_take_precedence_over_a_perfect_match():
    criteria = crit(titles=["Software Engineer"], keywords=["python"], exclude_keywords=["python"])
    result = score(criteria, title="Software Engineer", remote=True, description="python")
    assert result.disqualified is True
    assert result.score == 0


# ------------------------------------------------------------------- title --


def test_title_neutral_when_no_titles_wanted():
    assert points("title", crit(), title="Anything At All") == 25


def test_title_full_marks_on_title_match():
    criteria = crit(titles=["Software Engineer"])
    assert points("title", criteria, title="Senior Software Engineer, Payments") == 35


def test_title_partial_marks_on_half_the_words():
    criteria = crit(titles=["Senior Backend Engineer"])
    assert points("title", criteria, title="Backend Engineer II") == 20
    assert points("title", criteria, title="Backend Developer") == 0
    assert points("title", crit(titles=["Data Scientist"]), title="Data Analyst") == 20


def test_title_zero_when_nothing_matches():
    assert points("title", crit(titles=["Software Engineer"]), title="Product Manager") == 0


# ------------------------------------------------------------------ salary --


def test_salary_neutral_when_no_minimum():
    assert points("salary", crit(), salary_min=10, salary_max=20) == 15


def test_salary_unknown_scores_ten():
    assert points("salary", crit(salary_min=120000)) == 10


def test_salary_full_marks_when_a_bound_clears_the_floor():
    criteria = crit(salary_min=120000)
    assert points("salary", criteria, salary_min=100000, salary_max=150000) == 20
    assert points("salary", criteria, salary_min=120000, salary_max=None) == 20
    assert points("salary", criteria, salary_min=None, salary_max=120000) == 20


def test_salary_zero_when_only_a_low_minimum_is_known():
    assert points("salary", crit(salary_min=120000), salary_min=80000, salary_max=None) == 0


# ---------------------------------------------------------------- location --


def test_location_remote_flag_with_remote_ok():
    criteria = crit(locations=["Austin, TX"])
    assert points("location", criteria, remote=True, location=None) == 20
    assert points("location", criteria, remote=1, location="Berlin") == 20
    assert points("location", criteria, remote=False, location="Berlin") == 0


def test_location_remote_mention_in_location_or_title():
    criteria = crit(locations=["Austin, TX"])
    assert points("location", criteria, location="Remote - US") == 20
    assert points("location", criteria, title="Engineer (Remote)", location="Boston, MA") == 20
    assert points("location", criteria, location="Remotely nowhere") == 0


def test_location_remote_job_when_remote_not_ok():
    criteria = crit(remote_ok=False, locations=["Austin, TX"])
    assert points("location", criteria, remote=True, location="Remote") == 0
    assert points("location", criteria, remote=True, location="Austin, TX") == 20


def test_location_neutral_when_no_locations_wanted():
    assert points("location", crit(), location="Nowhere") == 15


def test_location_substring_and_every_word_match():
    assert points("location", crit(locations=["New York"]), location="New York, NY") == 20
    assert points("location", crit(locations=["New York, NY"]), location="NY, New York") == 20
    assert points("location", crit(locations=["bay area"]), location="Bay Area (SF)") == 20


def test_location_city_alone_matches_for_city_state_form():
    criteria = crit(locations=["Austin, TX"])
    assert points("location", criteria, location="Austin, Texas") == 20
    assert points("location", criteria, location="Austin") == 20
    assert points("location", criteria, location="Dallas, TX") == 0


def test_location_short_code_does_not_match_inside_a_word():
    assert points("location", crit(locations=["US"]), location="Austin, TX") == 0
    assert points("location", crit(remote_ok=False, locations=["US"]), location="Remote, US") == 20


def test_location_missing_scores_eight_and_mismatch_zero():
    criteria = crit(locations=["Austin, TX"])
    assert points("location", criteria, location=None) == 8
    assert points("location", criteria, location="") == 8
    assert points("location", criteria, location="Berlin, Germany") == 0


def test_location_remote_criterion_only_matches_remote_jobs():
    criteria = crit(locations=["Remote"])
    assert points("location", criteria, location="Austin, TX") == 0
    assert points("location", criteria, location=None) == 8
    assert points("location", criteria, location="Remote") == 20
    assert points("location", criteria, remote=True, location="Austin, TX") == 20
    no_remote = crit(remote_ok=False, locations=["Remote"])
    assert points("location", no_remote, remote=True, location="Remote") == 0


# ---------------------------------------------------------------- keywords --


def test_keywords_neutral_when_pool_empty():
    result = score(crit(), description="python aws")
    assert result.breakdown["keywords"] == 12
    assert "(" not in result.reason


def test_keywords_bands_scale_to_six_hits():
    pool = ["aws", "python", "terraform", "docker", "kubernetes", "postgres", "redis", "kafka"]
    criteria = crit(keywords=pool)

    def hits(n: int) -> int:
        return points("keywords", criteria, description=" ".join(pool[:n]))

    assert hits(0) == 0
    assert hits(1) == 4
    assert hits(2) == 8
    assert hits(3) == round(3 / 6 * 25)
    assert hits(4) == 17
    assert hits(5) == 21
    assert hits(6) == 25
    assert hits(8) == 25


def test_keywords_count_distinct_terms_once():
    result = score(crit(keywords=["python", "Python "]), description="python python python")
    assert result.breakdown["keywords"] == 4
    assert result.reason.endswith("(python)")


def test_keywords_pool_includes_profile_tags():
    result = score(
        crit(keywords=["aws"]),
        {"terraform", "platform", "  ", "rust"},
        title="Platform Engineer",
        description="Terraform and AWS every day.",
    )
    assert result.breakdown["keywords"] == round(3 / 6 * 25)
    assert result.reason.endswith("(aws, platform, terraform)")


def test_keywords_whole_word_for_single_tokens():
    assert points("keywords", crit(keywords=["go"]), description="Google Cloud") == 0
    assert points("keywords", crit(keywords=["go"]), description="Written in Go.") == 4
    assert points("keywords", crit(keywords=["c++"]), description="C++ and Rust") == 4
    assert points("keywords", crit(keywords=["c"]), description="C++ and Rust") == 0
    assert points("keywords", crit(keywords=["c#", ".net"]), description="C#/.NET stack") == 8
    assert points("keywords", crit(keywords=["node.js"]), description="Node.js services") == 4
    assert points("keywords", crit(keywords=["node.js"]), description="nodejs services") == 0


def test_keywords_phrase_matches_as_substring():
    criteria = crit(keywords=["machine learning"])
    assert points("keywords", criteria, title="Machine Learning Engineer") == 4
    assert points("keywords", criteria, description="machine\nlearning models") == 4
    assert points("keywords", criteria, description="learning about machines") == 0


def test_keywords_reason_lists_at_most_eight_sorted():
    pool = [f"term{i:02d}" for i in range(12)]
    result = score(crit(keywords=pool), description=" ".join(reversed(pool)))
    assert result.breakdown["keywords"] == 25
    assert result.reason.endswith("(" + ", ".join(pool[:8]) + ")")


# ----------------------------------------------------------------- overall --


def test_missing_fields_are_tolerated():
    result = score_rules({}, SearchCriteria(), set())
    assert result.disqualified is False
    assert result.breakdown == {"title": 25, "salary": 15, "location": 8, "keywords": 12}
    assert result.score == 60
    assert result.reason == "title 25/35 · salary 15/20 · location 8/20 · keywords 12/25"


def test_none_fields_are_tolerated_with_every_criterion_set():
    result = score_rules(
        {"title": None, "company": None, "location": None, "description": None, "remote": None},
        crit(
            titles=["Engineer"],
            locations=["Austin, TX"],
            keywords=["python"],
            exclude_keywords=["sales"],
            company_blacklist=["Acme"],
            salary_min=100000,
        ),
        set(),
    )
    assert result.disqualified is False
    assert result.breakdown == {"title": 0, "salary": 10, "location": 8, "keywords": 0}


def test_full_marks_and_reason_format():
    result = score(
        crit(titles=["Software Engineer"], keywords=["aws", "python", "terraform", "docker"]),
        title="Software Engineer",
        remote=True,
        salary_min=150000,
        salary_max=180000,
        description="Python, AWS, Terraform and Docker.",
    )
    assert result.reason == (
        "title 35/35 · salary 15/20 · location 20/20 · "
        "keywords 17/25 (aws, docker, python, terraform)"
    )
    assert result.score == 87


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"remote": True, "description": "python go rust"},
        {"title": "Backend Engineer", "location": "Austin", "salary_max": 200000},
        {"title": None, "location": "Berlin", "salary_min": 10000},
    ],
)
def test_breakdown_sums_to_score(fields: dict[str, Any]):
    criteria = crit(
        titles=["Senior Backend Engineer"],
        locations=["Austin, TX"],
        keywords=["python", "go"],
        salary_min=120000,
    )
    result = score(criteria, {"rust"}, **fields)
    assert result.score == sum(result.breakdown.values())
    assert 0 <= result.score <= 100


def test_passes_applies_the_threshold_and_disqualification():
    criteria = crit(min_score=60)
    assert passes(RuleScore(score=60, disqualified=False, reason=""), criteria) is True
    assert passes(RuleScore(score=59, disqualified=False, reason=""), criteria) is False
    assert passes(RuleScore(score=0, disqualified=True, reason=""), crit(min_score=0)) is False
    assert passes(score_rules({}, SearchCriteria(), set()), SearchCriteria()) is True
