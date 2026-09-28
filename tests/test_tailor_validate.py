from __future__ import annotations

from typing import Any

from jobagent.resume.facts import Fact
from jobagent.tailor.models import Bullet, CoverLetter, Entry, Issue, TailoredResume
from jobagent.tailor.validate import (
    INACTIVE_FACT,
    NEVER_CLAIM,
    NUMBER_NOT_IN_FACT,
    TERM_NOT_IN_ANY_FACT,
    TERM_NOT_IN_CITED_FACT,
    UNKNOWN_FACT,
    fact_pool_text,
    validate_cover_letter,
    validate_resume,
)

ACME = {"employer": "Acme Corp", "title": "Senior Engineer"}


def fact(
    kind: str, text: str, *, tags: list[str] | None = None, active: bool = True, **detail: Any
) -> Fact:
    return Fact(kind=kind, text=text, detail=detail, tags=tags or [], active=active)


def facts() -> dict[int, Fact]:
    """A small fact base: two Acme role facts, one Globex, a project, and inactive rows."""
    return {
        1: fact(
            "role",
            "Led migration of the billing service from a monolith to Python microservices "
            "on AWS, cutting p99 latency 40% and saving $1.2M a year.",
            tags=["python", "aws"],
            start="2019-03",
            end="present",
            **ACME,
        ),
        2: fact(
            "role",
            "Ran the on-call rotation for 3 teams and wrote the PostgreSQL runbooks.",
            tags=["postgresql"],
            **ACME,
        ),
        3: fact(
            "role",
            "Built a C++ trading gateway and its Node.js dashboard with CI/CD on Jenkins.",
            tags=["c++", "node.js"],
            employer="Globex",
            title="Engineer",
        ),
        4: fact("project", "Open-source Rust CLI for parsing invoices, 3,000 downloads."),
        5: fact("skill", "Python"),
        6: fact("skill", "PostgreSQL"),
        7: fact("education", "BSc Computer Science, MIT, 2015", school="MIT"),
        8: fact("credential", "AWS Certified Solutions Architect"),
        9: fact("summary", "Backend engineer with eight years of experience."),
        10: fact("skill", "Terraform", active=False),
        11: fact(
            "role",
            "Managed Kubernetes clusters for Initech.",
            employer="Initech",
            title="SRE",
            active=False,
        ),
    }


def entry(fact_ids: list[int], *bullets: tuple[int, str]) -> Entry:
    return Entry(fact_ids=fact_ids, bullets=[Bullet(fact_id=f, text=t) for f, t in bullets])


def plan(**overrides: Any) -> TailoredResume:
    """A plan that cites every kind of fact and rephrases each within its fact."""
    base: dict[str, Any] = {
        "summary": "Backend engineer with Python and AWS experience, cutting latency 40%.",
        "skills": [5, 6],
        "experience": [
            entry(
                [1, 2],
                (1, "Migrated billing to Python microservices on AWS, cutting p99 latency 40%."),
                (2, "Ran on-call for 3 teams and wrote PostgreSQL runbooks."),
            ),
            entry([3], (3, "Built a C++ trading gateway and a Node.js dashboard with CI/CD.")),
        ],
        "projects": [
            entry(
                [4], (4, "Wrote an open-source Rust CLI for invoice parsing with 3,000 downloads.")
            )
        ],
        "education": [7],
        "credentials": [8],
    }
    base.update(overrides)
    return TailoredResume(**base)


def check(
    p: TailoredResume,
    never: list[str] | None = None,
    posting: list[str] | None = None,
    base: dict[int, Fact] | None = None,
) -> list[Issue]:
    return validate_resume(p, facts() if base is None else base, never or [], posting or [])


def letter(*paragraphs: str) -> CoverLetter:
    return CoverLetter(paragraphs=list(paragraphs))


ALLOW = ["Globex Dynamics", "Staff Backend Engineer", "Jane Doe"]


def check_letter(
    lt: CoverLetter,
    never: list[str] | None = None,
    posting: list[str] | None = None,
    allow: list[str] | None = None,
) -> list[Issue]:
    return validate_cover_letter(
        lt, facts(), never or [], posting or [], ALLOW if allow is None else allow
    )


def only(issues: list[Issue], where: str, message: str, **fields: Any) -> Issue:
    """The one issue at `where` with `message`; fails loudly when there is not exactly one."""
    found = [i for i in issues if i.where == where and i.message == message]
    assert len(found) == 1, f"expected one {message!r} at {where}, got {issues}"
    for name, value in fields.items():
        assert getattr(found[0], name) == value, f"{name}: {found[0]}"
    return found[0]


def places(issues: list[Issue]) -> list[str]:
    return list(dict.fromkeys(i.where for i in issues))


# ------------------------------------------------------------------ clean --


def test_clean_plan_has_no_issues():
    assert check(plan()) == []


def test_clean_plan_survives_never_claim_and_posting_terms_it_does_not_lift():
    issues = validate_resume(
        plan(), facts(), (t for t in ["kubernetes", "clearance"]), (t for t in ["python", "go"])
    )
    assert issues == []


def test_empty_plan_has_no_issues():
    assert check(TailoredResume()) == []


# ------------------------------------------------------------------- pool --


def test_fact_pool_text_lists_text_tags_and_detail_values():
    f = fact("role", "Shipped it", tags=["python", "aws"], **ACME, start="2019-03")
    assert fact_pool_text([f]) == "Shipped it\npython\naws\nAcme Corp\nSenior Engineer\n2019-03"


def test_fact_pool_text_flattens_nested_and_non_string_detail():
    f = fact(
        "education",
        "BSc",
        year=2015,
        honours=None,
        courses=["Algorithms", "Databases"],
        extra={"gpa": 3.9, "dean": True, "blank": "  "},
    )
    assert fact_pool_text([f, fact("skill", "Rust")]).split("\n") == [
        "BSc",
        "2015",
        "Algorithms",
        "Databases",
        "3.9",
        "Rust",
    ]


def test_fact_pool_text_of_nothing_is_empty():
    assert fact_pool_text([]) == ""


# ------------------------------------------------------------- S1 existence --


def test_unknown_fact_is_reported_where_it_is_cited():
    issues = check(
        plan(
            skills=[5, 99],
            experience=[entry([1, 98], (1, "Migrated billing to Python."), (97, "Something."))],
            education=[96],
            credentials=[95],
        )
    )
    only(issues, "skills[1]", UNKNOWN_FACT, fact_id=99)
    only(issues, "experience[0]", UNKNOWN_FACT, fact_id=98)
    only(issues, "experience[0].bullets[1]", UNKNOWN_FACT, fact_id=97)
    only(issues, "education[0]", UNKNOWN_FACT, fact_id=96)
    only(issues, "credentials[0]", UNKNOWN_FACT, fact_id=95)


def test_inactive_fact_is_reported_as_inactive_not_unknown():
    issues = check(plan(skills=[5, 10]))
    only(issues, "skills[1]", INACTIVE_FACT, fact_id=10)
    assert [i for i in issues if i.message == UNKNOWN_FACT] == []

    issues = check(plan(experience=[entry([11], (11, "Managed Kubernetes clusters."))]))
    assert issues == [Issue("experience[0]", INACTIVE_FACT, fact_id=11)]


# ------------------------------------------------------------------ S2 kinds --


def test_each_section_accepts_only_its_kind():
    issues = check(
        plan(
            skills=[1],
            education=[8],
            credentials=[7],
            experience=[entry([4], (4, "Wrote a Rust CLI."))],
            projects=[entry([3], (3, "Built a C++ gateway."))],
        )
    )
    only(issues, "skills[0]", "expected a skill fact, got role", fact_id=1)
    only(issues, "education[0]", "expected a education fact, got credential", fact_id=8)
    only(issues, "credentials[0]", "expected a credential fact, got education", fact_id=7)
    only(issues, "experience[0]", "expected a role fact, got project", fact_id=4)
    only(issues, "projects[0]", "expected a project fact, got role", fact_id=3)
    assert len(issues) == 5


# --------------------------------------------------------------- S3 heading --


def test_entry_facts_must_share_employer_and_title():
    issues = check(plan(experience=[entry([1, 3], (1, "Migrated billing to Python."))]))
    only(issues, "experience[0]", "entry mixes facts from different employers")
    only(issues, "experience[0]", "entry mixes facts with different titles")

    base = facts()
    base[12] = fact("role", "Fixed the build.", employer="Acme Corp", title="Engineer")
    issues = check(plan(experience=[entry([1, 12], (12, "Fixed the build."))]), base=base)
    assert issues == [Issue("experience[0]", "entry mixes facts with different titles")]


def test_employer_and_title_compare_ignoring_case_and_whitespace():
    base = facts()
    base[12] = fact("role", "Fixed the build.", employer=" acme corp ", title="SENIOR ENGINEER")
    assert check(plan(experience=[entry([1, 12], (12, "Fixed the build."))]), base=base) == []


def test_entry_without_employer_or_title_holds_exactly_one_fact():
    base = facts()
    base[13] = fact("project", "Wrote a Haskell parser.")
    issues = check(
        plan(projects=[entry([4, 13], (4, "Wrote a Rust CLI."), (13, "Wrote a Haskell parser."))]),
        base=base,
    )
    assert issues == [
        Issue("projects[0]", "facts without an employer or title cannot share an entry")
    ]
    assert check(plan(projects=[entry([13], (13, "Wrote a Haskell parser."))]), base=base) == []

    # A title alone is enough to group by.
    base[14] = fact("project", "Wrote the Rust tests.", title="Invoice CLI")
    base[15] = fact("project", "Wrote the Rust docs.", title="Invoice CLI")
    assert check(plan(projects=[entry([14, 15], (14, "Wrote the Rust tests."))]), base=base) == []


# ---------------------------------------------------------------- S4 citing --


def test_bullet_must_cite_a_fact_of_its_entry():
    text = "Ran on-call for 3 teams and wrote PostgreSQL runbooks."
    issues = check(plan(experience=[entry([1], (1, "Migrated billing to Python."), (2, text))]))
    assert issues == [
        Issue("experience[0].bullets[1]", "bullet cites a fact outside its entry", fact_id=2)
    ]


# --------------------------------------------------------------- S5 repeats --


def test_fact_may_not_appear_in_two_entries():
    issues = check(
        plan(
            experience=[
                entry([1], (1, "Migrated billing to Python.")),
                entry([1], (1, "Cut p99 latency 40%.")),
            ]
        )
    )
    assert issues == [Issue("experience[1]", "fact already used in experience[0]", fact_id=1)]


def test_repeated_ids_within_a_list_or_entry():
    issues = check(
        plan(
            skills=[5, 6, 5],
            education=[7, 7],
            experience=[entry([1, 1], (1, "Migrated billing to Python."))],
        )
    )
    only(issues, "skills[2]", "fact repeated", fact_id=5)
    only(issues, "education[1]", "fact repeated", fact_id=7)
    only(issues, "experience[0]", "fact repeated", fact_id=1)
    assert len(issues) == 3


def test_entry_needs_facts():
    issues = check(plan(experience=[entry([], (1, "Migrated billing to Python."))]))
    only(issues, "experience[0]", "entry has no facts")
    only(issues, "experience[0].bullets[0]", "bullet cites a fact outside its entry", fact_id=1)


# ----------------------------------------------------------------- S6 sizes --


def test_entry_needs_a_bullet():
    assert check(plan(experience=[entry([3])])) == [Issue("experience[0]", "entry has no bullets")]


def test_blank_and_overlong_bullets():
    issues = check(plan(experience=[entry([3], (3, "   "), (3, "x" * 301), (3, "x" * 300))]))
    assert issues == [
        Issue("experience[0].bullets[0]", "blank bullet"),
        Issue("experience[0].bullets[1]", "bullet longer than 300 characters"),
    ]


def test_summary_length_limit():
    assert check(plan(summary="y" * 601)) == [
        Issue("summary", "summary longer than 600 characters")
    ]
    assert check(plan(summary="y" * 600)) == []
    assert check(plan(summary="   ")) == []


# ------------------------------------------------------------ C1 never-claim --


def test_never_claim_term_is_reported_as_such_and_only_once():
    where = "experience[0].bullets[0]"
    issues = check(
        plan(experience=[entry([1], (1, "Ran Kubernetes for billing on AWS."))]),
        never=["Kubernetes"],
    )
    assert issues == [Issue(where, NEVER_CLAIM, term="Kubernetes")]

    issues = check(plan(summary="Holds a security  clearance."), never=["security clearance"])
    assert issues == [Issue("summary", NEVER_CLAIM, term="security clearance")]


def test_never_claim_matches_whole_words_only():
    assert check(plan(), never=["python3", "aw"]) == []


# ---------------------------------------------------------------- C2 numbers --


def test_number_missing_from_the_cited_fact():
    where = "experience[0].bullets[0]"
    issues = check(plan(experience=[entry([1], (1, "Cut latency 25% and costs $2M on AWS."))]))
    assert issues == [
        Issue(where, NUMBER_NOT_IN_FACT, term="$2m"),
        Issue(where, NUMBER_NOT_IN_FACT, term="25%"),
    ]


def test_number_normalisation_and_sentence_ending_numbers():
    bullets = [
        (1, "Saved $1.2m a year on AWS."),
        (1, "Cut p99 latency 40%."),
        (1, "Cut p99 latency by 40% since 2019."),
    ]
    assert check(plan(experience=[entry([1], *bullets)])) == []
    issues = check(plan(projects=[entry([4], (4, "Rust CLI with 3000 downloads."))]))
    assert issues == []
    issues = check(plan(projects=[entry([4], (4, "Rust CLI with 30,000 downloads."))]))
    assert issues == [Issue("projects[0].bullets[0]", NUMBER_NOT_IN_FACT, term="30000")]


def test_bullet_numbers_come_from_the_cited_fact_not_a_sibling():
    where = "experience[0].bullets[0]"
    text = "Migrated billing to Python and wrote PostgreSQL runbooks for 3 teams."
    issues = check(plan(experience=[entry([1, 2], (1, text))]))
    assert issues == [Issue(where, NUMBER_NOT_IN_FACT, term="3")]


def test_summary_numbers_may_come_from_any_active_fact():
    assert check(plan(summary="Engineer since 2019: 3,000 downloads and 40% faster.")) == []
    assert check(plan(summary="Cut costs 60%.")) == [
        Issue("summary", NUMBER_NOT_IN_FACT, term="60%")
    ]


# ------------------------------------------------------------------ C3 terms --


def test_postgres_is_not_supported_by_postgresql():
    where = "experience[0].bullets[0]"
    issues = check(plan(experience=[entry([2], (2, "Moved the schema to Postgres."))]))
    assert issues == [Issue(where, TERM_NOT_IN_CITED_FACT, term="postgres")]
    assert check(plan(experience=[entry([2], (2, "Moved the schema to PostgreSQL."))])) == []
    assert check(plan(experience=[entry([2], (2, "Moved the schema to postgresql."))])) == []


def test_lowercase_name_is_caught_through_the_posting_terms():
    p = plan(experience=[entry([2], (2, "Moved the schema to postgres."))])
    # Plain lowercase prose is not name-like, so only the posting list catches it.
    assert check(p) == []
    assert check(p, posting=["Postgres"]) == [
        Issue("experience[0].bullets[0]", TERM_NOT_IN_CITED_FACT, term="postgres")
    ]


def test_symbol_terms():
    where = "experience[0].bullets[0]"
    ok = "Built a C++ trading gateway and Node.js dashboard with CI/CD."
    assert check(plan(experience=[entry([3], (3, ok))])) == []

    issues = check(plan(experience=[entry([3], (3, "Built a C# gateway in Go."))]))
    assert issues == [
        Issue(where, TERM_NOT_IN_CITED_FACT, term="c#"),
        Issue(where, TERM_NOT_IN_CITED_FACT, term="go"),
    ]
    issues = check(plan(experience=[entry([1], (1, "Wrote C++ services with CI/CD."))]))
    assert issues == [
        Issue(where, TERM_NOT_IN_CITED_FACT, term="c++"),
        Issue(where, TERM_NOT_IN_CITED_FACT, term="ci/cd"),
    ]


def test_posting_term_lifted_into_a_bullet_is_reported():
    where = "experience[0].bullets[0]"
    text = "Ran the Kubernetes rota with some machine  learning for 3 teams."
    issues = check(
        plan(experience=[entry([2], (2, text))]), posting=["kubernetes", "machine learning"]
    )
    assert issues == [
        Issue(where, TERM_NOT_IN_CITED_FACT, term="kubernetes"),
        Issue(where, TERM_NOT_IN_CITED_FACT, term="machine learning"),
    ]
    assert check(plan(), posting=["postgresql", "aws", "c++"]) == []


def test_posting_stopwords_are_never_lifted():
    assert (
        check(
            plan(summary="Backend engineer with Python experience."),
            posting=["experience", "engineer"],
        )
        == []
    )


def test_sibling_facts_in_an_entry_support_each_other():
    text = "Migrated billing to Python on AWS and wrote the PostgreSQL runbooks."
    assert check(plan(experience=[entry([1, 2], (1, text))])) == []
    assert check(plan(experience=[entry([1], (1, text))])) == [
        Issue("experience[0].bullets[0]", TERM_NOT_IN_CITED_FACT, term="postgresql")
    ]


def test_employer_title_and_date_words_are_always_allowed():
    base = facts()
    base[12] = fact(
        "role",
        "Shipped the ledger rewrite.",
        employer="Wayne Enterprises",
        title="Staff Engineer",
        start="2019-03",
    )
    text = (
        "Shipped the ledger rewrite at Wayne Enterprises as Staff Engineer, March 2019 to Present."
    )
    assert check(plan(experience=[entry([12], (12, text))]), base=base) == []


def test_summary_draws_on_any_active_fact_but_not_an_inactive_one():
    assert (
        check(plan(summary="Engineer with Rust, PostgreSQL and AWS Certified credentials.")) == []
    )
    issues = check(plan(summary="Engineer who ran Terraform and Kubernetes."))
    assert issues == [
        Issue("summary", TERM_NOT_IN_ANY_FACT, term="terraform"),
        Issue("summary", TERM_NOT_IN_ANY_FACT, term="kubernetes"),
    ]


def test_same_term_is_reported_once_per_place():
    where = "experience[0].bullets[0]"
    text = "Kubernetes here, Kubernetes there, KUBERNETES everywhere."
    issues = check(plan(experience=[entry([1], (1, text))]), posting=["Kubernetes"])
    assert issues == [Issue(where, TERM_NOT_IN_CITED_FACT, term="kubernetes")]


def test_bullet_of_an_inactive_fact_is_still_checked_against_it():
    issues = check(plan(experience=[entry([11], (11, "Managed Kubernetes clusters on GCP."))]))
    assert issues == [
        Issue("experience[0]", INACTIVE_FACT, fact_id=11),
        Issue("experience[0].bullets[0]", TERM_NOT_IN_CITED_FACT, term="gcp"),
    ]


def test_where_paths_are_exact_and_in_page_order():
    base = facts()
    base[12] = fact("role", "Fixed the build.", employer="Acme Corp", title="Engineer")
    issues = check(
        plan(
            summary="Cut costs 60%.",
            skills=[5, 99],
            experience=[
                entry([1, 12], (1, "Migrated billing to Python."), (1, "Wrote C# services."))
            ],
            projects=[entry([4], (4, "Ported the Rust CLI to Haskell."))],
            education=[8],
            credentials=[7],
        ),
        base=base,
    )
    assert places(issues) == [
        "summary",
        "skills[1]",
        "experience[0]",
        "experience[0].bullets[1]",
        "projects[0].bullets[0]",
        "education[0]",
        "credentials[0]",
    ]
    assert [i.term for i in issues if i.term] == ["60%", "c#", "haskell"]


# ------------------------------------------------------------- cover letter --

CLEAN_LETTER = letter(
    "Dear Hiring Manager,",
    "I am excited to apply for the Staff Backend Engineer role at Globex Dynamics. At Acme "
    "Corp I cut p99 latency 40% with Python on AWS and wrote the PostgreSQL runbooks.",
    "My open-source Rust CLI has 3,000 downloads, and I hold the AWS Certified Solutions "
    "Architect credential from March 2019 to present.",
    "Sincerely,\nJane Doe",
)


def test_clean_letter_has_no_issues():
    assert check_letter(CLEAN_LETTER) == []


def test_letter_may_name_company_and_title_only_through_allow():
    issues = check_letter(CLEAN_LETTER, allow=[])
    assert issues == [
        Issue("cover_letter[1]", TERM_NOT_IN_ANY_FACT, term="dynamics"),
        Issue("cover_letter[3]", TERM_NOT_IN_ANY_FACT, term="jane"),
        Issue("cover_letter[3]", TERM_NOT_IN_ANY_FACT, term="doe"),
    ]


def test_letter_may_not_claim_a_technology_the_facts_lack():
    lt = letter("Dear Hiring Manager,", "I also ran Kubernetes and Terraform at scale.", "Jane Doe")
    assert check_letter(lt, posting=["kubernetes"]) == [
        Issue("cover_letter[1]", TERM_NOT_IN_ANY_FACT, term="kubernetes"),
        Issue("cover_letter[1]", TERM_NOT_IN_ANY_FACT, term="terraform"),
    ]


def test_letter_paragraph_count():
    where = "cover_letter"
    assert check_letter(letter("Jane Doe")) == [
        Issue(where, "cover letter needs 2 to 6 paragraphs, got 1")
    ]
    assert check_letter(letter(*["Jane Doe"] * 7)) == [
        Issue(where, "cover letter needs 2 to 6 paragraphs, got 7")
    ]
    assert check_letter(letter(*["Jane Doe"] * 2)) == []
    assert check_letter(letter(*["Jane Doe"] * 6)) == []
    assert check_letter(letter()) == [Issue(where, "cover letter needs 2 to 6 paragraphs, got 0")]


def test_letter_blank_and_overlong_paragraphs():
    assert check_letter(letter("Jane Doe", " ", "z" * 901, "z" * 900)) == [
        Issue("cover_letter[1]", "blank paragraph"),
        Issue("cover_letter[2]", "paragraph longer than 900 characters"),
    ]


def test_letter_never_claim_and_numbers():
    lt = letter("I hold a clearance.", "I saved $5M and 40% at Acme Corp.")
    assert check_letter(lt, never=["clearance"]) == [
        Issue("cover_letter[0]", NEVER_CLAIM, term="clearance"),
        Issue("cover_letter[1]", NUMBER_NOT_IN_FACT, term="$5m"),
    ]
    # A number in the company name is one the letter may say.
    assert check_letter(letter("Jane Doe", "I want to join 3M."), allow=["3M", "Jane Doe"]) == []
