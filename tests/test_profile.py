"""The profile view of the fact base: the tag pool and the plain-text summary."""

from __future__ import annotations

import pytest

from jobagent.resume.facts import Fact, add_fact, add_facts, set_active
from jobagent.resume.profile import profile_tags, profile_text


def _seed(conn) -> None:
    add_facts(
        conn,
        [
            Fact(kind="credential", text="AWS Solutions Architect"),
            Fact(kind="skill", text="Kubernetes", tags=["kubernetes", "K8s"]),
            Fact(kind="education", text="BSc Computer Science, Somewhere University"),
            Fact(
                kind="role",
                text="Led the payments platform team",
                detail={
                    "title": "Staff Engineer",
                    "employer": "Acme",
                    "start": "2020",
                    "end": "2023",
                },
                tags=["Python", "payments"],
            ),
            Fact(kind="summary", text="Backend engineer with ten years in fintech."),
            Fact(kind="skill", text="python"),
            Fact(
                kind="project",
                text="Open-source rate limiter with 2k stars",
                detail={"title": "ratelimitd"},
                tags=["go"],
            ),
            Fact(kind="skill", text=" AWS "),
        ],
    )


# ---------------------------------------------------------- profile_tags --


def test_profile_tags_collects_tags_and_skill_names_lowercased(conn):
    _seed(conn)
    assert profile_tags(conn) == {"kubernetes", "k8s", "python", "payments", "go", "aws"}


def test_profile_tags_does_not_include_non_skill_text(conn):
    add_fact(conn, Fact(kind="role", text="Ran Kafka for Acme"))
    add_fact(conn, Fact(kind="credential", text="CKA"))
    assert profile_tags(conn) == set()


def test_profile_tags_skips_inactive_facts(conn):
    keep = add_fact(conn, Fact(kind="skill", text="Rust", tags=["systems"]))
    drop = add_fact(conn, Fact(kind="skill", text="COBOL", tags=["mainframe"]))
    set_active(conn, drop, False)

    assert profile_tags(conn) == {"rust", "systems"}
    set_active(conn, keep, False)
    assert profile_tags(conn) == set()


# ---------------------------------------------------------- profile_text --


def test_profile_text_says_so_when_there_is_no_resume(conn):
    assert profile_text(conn) == "(no resume on file)"

    fact_id = add_fact(conn, Fact(kind="skill", text="Python"))
    set_active(conn, fact_id, False)
    assert profile_text(conn) == "(no resume on file)"


def test_profile_text_groups_by_kind_in_a_fixed_order(conn):
    _seed(conn)
    assert profile_text(conn) == (
        "Summary:\n"
        "- Backend engineer with ten years in fintech.\n"
        "\n"
        "Experience:\n"
        "- Staff Engineer · Acme (2020 – 2023): Led the payments platform team\n"
        "\n"
        "Projects:\n"
        "- ratelimitd: Open-source rate limiter with 2k stars\n"
        "\n"
        "Skills: AWS, Kubernetes, python\n"
        "\n"
        "Education:\n"
        "- BSc Computer Science, Somewhere University\n"
        "\n"
        "Credentials:\n"
        "- AWS Solutions Architect"
    )


def test_profile_text_omits_empty_sections(conn):
    add_fact(conn, Fact(kind="skill", text="Go"))
    add_fact(conn, Fact(kind="education", text="MSc"))
    assert profile_text(conn) == "Skills: Go\n\nEducation:\n- MSc"


def test_profile_text_role_lines_degrade_with_missing_detail(conn):
    add_facts(
        conn,
        [
            Fact(kind="role", text="Built the API", detail={"employer": "Globex"}),
            Fact(
                kind="role", text="Shipped the app", detail={"title": "iOS Lead", "start": "2019"}
            ),
            Fact(kind="role", text="Kept the lights on", detail={"start": "2015", "end": "2016"}),
            Fact(kind="role", text="Did things"),
        ],
    )
    assert profile_text(conn) == (
        "Experience:\n"
        "- Globex: Built the API\n"
        "- iOS Lead (2019): Shipped the app\n"
        "- Kept the lights on\n"
        "- Did things"
    )


def test_profile_text_dedupes_skills_and_sorts_them_case_insensitively(conn):
    add_facts(
        conn,
        [
            Fact(kind="skill", text="zsh"),
            Fact(kind="skill", text="Bash"),
            Fact(kind="skill", text="Bash", source="user_added"),
            Fact(kind="skill", text="awk"),
        ],
    )
    assert profile_text(conn) == "Skills: awk, Bash, zsh"


def test_profile_text_dedupes_skills_ignoring_case_with_the_first_spelling_winning(conn):
    add_facts(
        conn,
        [
            Fact(kind="skill", text="Python"),
            Fact(kind="skill", text="python", source="user_added"),
            Fact(kind="skill", text="PYTHON"),
            Fact(kind="skill", text="go"),
            Fact(kind="skill", text="Go"),
        ],
    )
    assert profile_text(conn) == "Skills: go, Python"


def test_profile_text_renders_numeric_years_in_role_details(conn):
    add_fact(
        conn,
        Fact(
            kind="role",
            text="Ran the data team",
            detail={"title": "Head of Data", "employer": "Globex", "start": 2020, "end": 2021},
        ),
    )
    assert profile_text(conn) == (
        "Experience:\n- Head of Data · Globex (2020 – 2021): Ran the data team"
    )


@pytest.mark.parametrize("detail", [["Staff Engineer", "Acme"], "Staff Engineer at Acme", 7])
def test_profile_text_ignores_a_detail_that_is_not_a_mapping(conn, detail):
    add_fact(conn, Fact(kind="role", text="Built the API", detail=detail))  # type: ignore[arg-type]
    assert profile_text(conn) == "Experience:\n- Built the API"


def test_profile_text_caps_each_section(conn):
    add_facts(conn, [Fact(kind="credential", text=f"Cert {n:03d}") for n in range(65)])
    lines = profile_text(conn).splitlines()
    assert lines[0] == "Credentials:"
    assert len(lines) == 61
    assert lines[-1] == "- Cert 059"
