"""The renderer: a plan and its facts on an ATS-safe page, as HTML, text and PDF."""

from __future__ import annotations

import importlib.resources
import tomllib
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from jobagent.resume.facts import Fact
from jobagent.tailor.models import Bullet, CoverLetter, Entry, TailoredResume
from jobagent.tailor.render import (
    cover_letter_html,
    render_pdf,
    resume_html,
    resume_text,
    write_pdf,
)

HEADINGS = ("Summary", "Skills", "Experience", "Projects", "Education", "Certifications")

CONTACT = {
    "full_name": "Jane Doe",
    "email": "jane@example.com",
    "phone": "555-0100",
    "location": "Austin, TX",
    "links": ["https://github.com/jane", "https://jane.dev"],
}


def fact(kind: str, text: str, **detail: Any) -> Fact:
    return Fact(kind=kind, text=text, detail=detail)


def facts() -> dict[int, Fact]:
    return {
        1: fact(
            "role",
            "Led the billing migration to Python microservices on AWS.",
            employer="Acme Corp",
            title="Senior Engineer",
            start="2019-03",
            end="present",
        ),
        2: fact(
            "role",
            "Ran the on-call rotation for 3 teams.",
            employer="Acme Corp",
            title="Senior Engineer",
        ),
        3: fact("role", "Built a C++ trading gateway.", employer="Globex", title="Engineer"),
        4: fact("project", "Open-source Rust CLI for parsing invoices.", title="invoice-cli"),
        5: fact("skill", "Python"),
        6: fact("skill", "PostgreSQL"),
        7: fact("skill", "AWS"),
        8: fact("education", "BSc Computer Science", employer="MIT", start="2011", end="2015"),
        9: fact("credential", "AWS Certified Solutions Architect", start="2022"),
    }


def plan() -> TailoredResume:
    return TailoredResume(
        summary="Backend engineer focused on Python services on AWS.",
        skills=[5, 6, 7],
        experience=[
            Entry(
                fact_ids=[1, 2],
                bullets=[
                    Bullet(fact_id=1, text="Migrated billing to Python microservices on AWS."),
                    Bullet(fact_id=2, text="Ran on-call for 3 teams."),
                ],
            ),
            Entry(fact_ids=[3], bullets=[Bullet(fact_id=3, text="Built a C++ trading gateway.")]),
        ],
        projects=[
            Entry(fact_ids=[4], bullets=[Bullet(fact_id=4, text="Rust CLI that parses invoices.")])
        ],
        education=[8],
        credentials=[9],
        emphasis="Leads with the AWS migration.",
    )


def entry_html(**detail: Any) -> str:
    """A resume with one experience entry whose heading fact has exactly `detail`."""
    base = {1: fact("role", "Did the thing.", **detail)}
    one = TailoredResume(
        experience=[Entry(fact_ids=[1], bullets=[Bullet(fact_id=1, text="Did the thing.")])]
    )
    return resume_html(one, base, {})


# ---------------------------------------------------------------- header --


def test_header_renders_name_and_contact_line():
    out = resume_html(plan(), facts(), CONTACT)
    assert "<h1>Jane Doe</h1>" in out
    assert (
        "jane@example.com | 555-0100 | Austin, TX | https://github.com/jane | https://jane.dev"
        in out
    )


def test_header_omits_missing_keys_without_rendering_none():
    out = resume_html(plan(), facts(), {"full_name": "Jane Doe", "email": "jane@example.com"})
    assert "<h1>Jane Doe</h1>" in out
    assert "jane@example.com</p>" in out
    assert "None" not in out
    assert " | " not in out


def test_header_skips_blank_and_null_values():
    contact = {"full_name": "  ", "email": None, "phone": " 555-0100 ", "links": ""}
    out = resume_html(plan(), facts(), contact)
    assert "<h1>" not in out
    assert ">555-0100</p>" in out
    assert "None" not in out


def test_links_accept_a_single_string():
    out = resume_html(plan(), facts(), {"email": "j@x.io", "links": "https://jane.dev"})
    assert "j@x.io | https://jane.dev" in out


def test_links_of_any_other_type_never_raise():
    assert "j@x.io | 42</p>" in resume_html(plan(), facts(), {"email": "j@x.io", "links": 42})
    assert "j@x.io | a | b</p>" in resume_html(
        plan(), facts(), {"email": "j@x.io", "links": ("a", "b")}
    )
    # A mapping is not a link; it is dropped rather than rendered as its keys or repr.
    out = resume_html(plan(), facts(), {"email": "j@x.io", "links": {"site": "https://dropped"}})
    assert '<p class="contact">j@x.io</p>' in out
    assert "site" not in out and "dropped" not in out


def test_target_title_line_only_when_given():
    without = resume_html(plan(), facts(), CONTACT)
    assert 'class="target"' not in without
    with_title = resume_html(plan(), facts(), CONTACT, target_title="Staff Engineer")
    assert '<p class="target">Staff Engineer</p>' in with_title
    # Under the name and contact line, above the first section.
    assert with_title.index("<h1>") < with_title.index('class="target"') < with_title.index("<h2>")


# -------------------------------------------------------------- sections --


def test_every_section_heading_in_page_order():
    out = resume_html(plan(), facts(), CONTACT)
    positions = [out.index(f"<h2>{h}</h2>") for h in HEADINGS]
    assert positions == sorted(positions)


def test_empty_plan_renders_no_section_headings():
    out = resume_html(TailoredResume(), facts(), CONTACT)
    assert "<h2>" not in out
    assert "<h1>Jane Doe</h1>" in out


def test_each_heading_appears_only_when_its_list_is_non_empty():
    full = plan()
    for heading, cleared in [
        ("Summary", {"summary": ""}),
        ("Skills", {"skills": []}),
        ("Experience", {"experience": []}),
        ("Projects", {"projects": []}),
        ("Education", {"education": []}),
        ("Certifications", {"credentials": []}),
    ]:
        out = resume_html(full.model_copy(update=cleared), facts(), CONTACT)
        assert f"<h2>{heading}</h2>" not in out, heading
        for other in HEADINGS:
            if other != heading:
                assert f"<h2>{other}</h2>" in out, (heading, other)


def test_whitespace_summary_counts_as_empty():
    out = resume_html(plan().model_copy(update={"summary": "  \n "}), facts(), CONTACT)
    assert "<h2>Summary</h2>" not in out


# --------------------------------------------------------------- entries --


def test_entry_heading_title_and_employer():
    assert "<h3>Senior Engineer — Acme Corp</h3>" in entry_html(
        title="Senior Engineer", employer="Acme Corp"
    )


def test_entry_heading_title_only():
    out = entry_html(title="Senior Engineer")
    assert "<h3>Senior Engineer</h3>" in out
    assert "—" not in out


def test_entry_heading_employer_only():
    out = entry_html(employer="Acme Corp")
    assert "<h3>Acme Corp</h3>" in out
    assert "—" not in out


def test_entry_without_title_or_employer_has_no_heading_but_keeps_bullets():
    out = entry_html()
    assert "<h3>" not in out
    assert "<li>Did the thing.</li>" in out


def test_entry_date_line_variants():
    both = entry_html(title="T", start="2019-03", end="present")
    assert '<p class="dates">2019-03 – present</p>' in both
    start_only = entry_html(title="T", start="2019-03")
    assert '<p class="dates">2019-03</p>' in start_only
    end_only = entry_html(title="T", end="2021")
    assert '<p class="dates">2021</p>' in end_only
    assert 'class="dates"' not in entry_html(title="T")


def test_entry_dates_from_numbers_render_without_none():
    out = entry_html(title="T", start=2019, end=None)
    assert '<p class="dates">2019</p>' in out
    assert "None" not in out


def test_bullets_render_in_order_and_heading_comes_from_first_fact():
    out = resume_html(plan(), facts(), CONTACT)
    first = out.index("Migrated billing to Python microservices on AWS.")
    second = out.index("Ran on-call for 3 teams.")
    third = out.index("Built a C++ trading gateway.")
    assert first < second < third
    assert out.index("<h3>Senior Engineer — Acme Corp</h3>") < first
    assert second < out.index("<h3>Engineer — Globex</h3>") < third
    assert "<h3>invoice-cli</h3>" in out


def test_cited_facts_without_a_bullet_are_not_rendered_as_text():
    out = resume_html(plan(), facts(), CONTACT)
    # Fact 1 is cited for the heading; only its bullet's rewording appears.
    assert "Led the billing migration" not in out


def test_entry_with_unknown_heading_fact_still_renders_bullets():
    orphan = TailoredResume(
        experience=[Entry(fact_ids=[99], bullets=[Bullet(fact_id=99, text="Still here.")])]
    )
    out = resume_html(orphan, facts(), CONTACT)
    assert "<li>Still here.</li>" in out
    assert "<h3>" not in out


def test_entry_with_no_fact_ids_renders_bullets_only():
    bare = TailoredResume(experience=[Entry(fact_ids=[], bullets=[Bullet(fact_id=1, text="X.")])])
    out = resume_html(bare, facts(), CONTACT)
    assert "<li>X.</li>" in out


def test_empty_entries_drop_their_section_heading_in_html_and_text():
    blank = Entry(fact_ids=[], bullets=[Bullet(fact_id=1, text="  ")])
    only_blank = TailoredResume(experience=[blank], projects=[blank])
    assert "<h2>" not in resume_html(only_blank, facts(), CONTACT)
    assert resume_text(only_blank, facts(), {}) == "\n"
    mixed = plan().model_copy(update={"experience": [blank, blank, *plan().experience]})
    text = resume_text(mixed, facts(), CONTACT)
    assert "\n\n\n" not in text
    assert "\nExperience\nSenior Engineer — Acme Corp\n" in text
    assert resume_html(mixed, facts(), CONTACT).count("<h3>") == 3


def test_container_detail_values_render_nothing():
    out = entry_html(title={"a": 1}, employer=["x", "y"], start="2019", end={"z": None})
    body = out.split("<body>")[1]
    assert "<h3>" not in body
    assert '<p class="dates">2019</p>' in body
    assert "{" not in body and "[" not in body and "None" not in body


# ---------------------------------------------------------------- skills --


def test_skills_are_one_comma_joined_line_in_plan_order():
    out = resume_html(plan().model_copy(update={"skills": [7, 5, 6]}), facts(), CONTACT)
    assert "<p>AWS, Python, PostgreSQL</p>" in out


def test_repeated_or_unknown_skill_ids_render_once_or_not_at_all():
    out = resume_html(plan().model_copy(update={"skills": [5, 5, 42, 6]}), facts(), CONTACT)
    assert "<p>Python, PostgreSQL</p>" in out


def test_blank_skill_text_leaves_no_empty_item():
    base = {5: fact("skill", "Python"), 6: fact("skill", "  \n"), 7: fact("skill", "AWS")}
    out = resume_html(TailoredResume(skills=[5, 6, 7]), base, {})
    assert "<p>Python, AWS</p>" in out


# ----------------------------------------------------- education, creds --


def test_education_line_with_employer_and_dates():
    out = resume_html(plan(), facts(), CONTACT)
    assert "<p>BSc Computer Science — MIT (2011 – 2015)</p>" in out


def test_education_line_without_employer_or_dates():
    base = {8: fact("education", "BSc Computer Science")}
    out = resume_html(TailoredResume(education=[8]), base, {})
    assert "<p>BSc Computer Science</p>" in out
    assert "—" not in out and "(" not in out


def test_education_line_employer_only_and_dates_only():
    base = {
        8: fact("education", "BSc", employer="MIT"),
        9: fact("credential", "CKA", start="2023"),
    }
    out = resume_html(TailoredResume(education=[8], credentials=[9]), base, {})
    assert "<p>BSc — MIT</p>" in out
    assert "<p>CKA (2023)</p>" in out


def test_credential_line_under_certifications():
    out = resume_html(plan(), facts(), CONTACT)
    line = out.index("<p>AWS Certified Solutions Architect (2022)</p>")
    assert out.index("<h2>Certifications</h2>") < line


# ---------------------------------------------------------------- safety --


def test_fact_and_contact_text_is_escaped():
    hostile = "<script>alert(1)</script>"
    base = {
        1: fact("role", hostile, title=hostile, employer="A & B"),
        2: fact("skill", hostile),
        3: fact("education", hostile),
    }
    evil = TailoredResume(
        summary=hostile,
        skills=[2],
        experience=[Entry(fact_ids=[1], bullets=[Bullet(fact_id=1, text=hostile)])],
        education=[3],
    )
    out = resume_html(evil, base, {"full_name": hostile, "email": hostile}, target_title=hostile)
    assert "<script>" not in out
    # <title>, name, contact line, target title, summary, skill, entry heading,
    # bullet and education line: nine places, all escaped.
    assert out.count("&lt;script&gt;alert(1)&lt;/script&gt;") == 9
    assert "A &amp; B" in out


def test_no_tables_images_or_floats():
    out = resume_html(plan(), facts(), CONTACT, target_title="Staff Engineer")
    lowered = out.lower()
    for forbidden in ("<table", "<img", "float:", "column-count", "<svg"):
        assert forbidden not in lowered, forbidden
    assert "Helvetica, Arial, sans-serif" in out
    assert "@page" in out and "0.6in" in out


def test_templates_are_package_data():
    """A wheel must carry the templates, or the first render raises TemplateNotFound."""
    package = importlib.resources.files("jobagent.tailor")
    for name in ("resume.html", "cover_letter.html"):
        assert (package / "templates" / name).is_file(), name
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    globs = config["tool"]["setuptools"]["package-data"]["jobagent"]
    for name in ("resume.html", "cover_letter.html"):
        assert any(fnmatch(f"tailor/templates/{name}", g) for g in globs), (name, globs)


# ------------------------------------------------------------ plain text --


def test_resume_text_has_everything_and_no_html():
    text = resume_text(plan(), facts(), CONTACT, target_title="Staff Engineer")
    assert "<" not in text
    for heading in HEADINGS:
        assert f"\n{heading}\n" in text
    for bullet in (
        "- Migrated billing to Python microservices on AWS.",
        "- Ran on-call for 3 teams.",
        "- Built a C++ trading gateway.",
        "- Rust CLI that parses invoices.",
    ):
        assert bullet in text
    assert "Python, PostgreSQL, AWS" in text
    assert "BSc Computer Science — MIT (2011 – 2015)" in text
    assert "AWS Certified Solutions Architect (2022)" in text


def test_resume_text_order_and_layout():
    text = resume_text(plan(), facts(), CONTACT, target_title="Staff Engineer")
    lines = text.splitlines()
    assert lines[:3] == [
        "Jane Doe",
        "jane@example.com | 555-0100 | Austin, TX | https://github.com/jane | https://jane.dev",
        "Staff Engineer",
    ]
    assert lines[3] == "" and lines[4] == "Summary"
    positions = [text.index(f"\n{h}\n") for h in HEADINGS]
    assert positions == sorted(positions)
    assert text.index("Senior Engineer — Acme Corp\n2019-03 – present\n- Migrated") > 0
    assert "\n\n\n" not in text
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_resume_text_omits_missing_header_and_sections():
    text = resume_text(TailoredResume(skills=[5]), facts(), {})
    assert text == "Skills\nPython\n"


def test_resume_text_matches_html_content():
    """The coverage gate measures the text; the PDF must say the same things."""
    text = resume_text(plan(), facts(), CONTACT)
    html = resume_html(plan(), facts(), CONTACT)
    for line in text.splitlines():
        assert line.removeprefix("- ") in html, line


def test_internal_newlines_collapse_so_every_text_line_is_whole():
    base = {
        1: fact("role", "a\nb", title="Senior\nEngineer", employer="Acme", start="2019\n"),
        2: fact("skill", "Py\nthon"),
        3: fact("skill", "AWS"),
        8: fact("education", "BSc\nCS", employer="M\nIT"),
    }
    plan_ = TailoredResume(
        summary="Backend\n\nengineer.",
        skills=[2, 3],
        experience=[Entry(fact_ids=[1], bullets=[Bullet(fact_id=1, text="Did\n  a\tthing.")])],
        education=[8],
    )
    contact = {"full_name": "Jane\nDoe", "location": "Austin,\nTX"}
    text = resume_text(plan_, base, contact, target_title="Staff\nEngineer")
    whole = {
        "Jane Doe",
        "Austin, TX",
        "Staff Engineer",
        "Backend engineer.",
        "Py thon, AWS",
        "Senior Engineer — Acme",
        "2019",
        "BSc CS — M IT",
    }
    for line in text.splitlines():
        assert line == "" or line in HEADINGS or line in whole or line == "- Did a thing.", line
    assert "- Did a thing." in text.splitlines()
    html = resume_html(plan_, base, contact, target_title="Staff\nEngineer")
    assert "<h1>Jane Doe</h1>" in html
    assert "<p>Py thon, AWS</p>" in html
    assert "<li>Did a thing.</li>" in html
    assert "\n" not in html.split("<h1>")[1].split("</h1>")[0]


# ---------------------------------------------------------- cover letter --


def test_cover_letter_renders_header_reference_and_paragraphs():
    letter = CoverLetter(paragraphs=["Dear team,", "I built <b>things</b>.", "Regards, Jane"])
    out = cover_letter_html(letter, CONTACT, company="Acme Corp", job_title="Staff Engineer")
    assert "<h1>Jane Doe</h1>" in out
    assert "jane@example.com | 555-0100 | Austin, TX" in out
    assert "Re: Staff Engineer at Acme Corp" in out
    assert "<p>Dear team,</p>" in out
    assert "<p>I built &lt;b&gt;things&lt;/b&gt;.</p>" in out
    assert "<p>Regards, Jane</p>" in out
    assert "<b>" not in out
    assert out.index("<h1>") < out.index("Re: ") < out.index("Dear team")


def test_cover_letter_reference_line_variants():
    letter = CoverLetter(paragraphs=["Hello."])
    assert "Re: " not in cover_letter_html(letter, CONTACT)
    assert "Re: Staff Engineer</p>" in cover_letter_html(
        letter, CONTACT, job_title="Staff Engineer"
    )
    assert "Re: Acme Corp</p>" in cover_letter_html(letter, CONTACT, company="Acme Corp")
    assert "Re: Staff Engineer at A &amp; B</p>" in cover_letter_html(
        letter, CONTACT, company="A & B", job_title="Staff Engineer"
    )


def test_cover_letter_skips_blank_paragraphs_and_has_no_date():
    letter = CoverLetter(paragraphs=["One.", "   ", "Two."])
    out = cover_letter_html(letter, {})
    assert out.count("<p>") == 2
    assert "<h1>" not in out
    assert "2026" not in out and "date" not in out.lower().split("<body>")[1]
    assert "<table" not in out and "<img" not in out


def test_cover_letter_shares_page_rule_fonts_and_sizes_with_resume():
    letter = cover_letter_html(CoverLetter(paragraphs=["Dear\nteam,"]), CONTACT)
    resume = resume_html(plan(), facts(), CONTACT)
    for rule in (
        "@page { size: Letter; margin: 0.6in; }",
        "body { font-family: Helvetica, Arial, sans-serif; font-size: 10.5pt;",
        "h1 { font-size: 18pt;",
    ):
        assert rule in letter and rule in resume, rule
    for forbidden in ("<table", "<img", "float:", "column-count", "<svg"):
        assert forbidden not in letter.lower(), forbidden
    assert "<p>Dear team,</p>" in letter


# ------------------------------------------------------------------- pdf --


def test_render_pdf_returns_pdf_bytes():
    html = resume_html(plan(), facts(), CONTACT, target_title="Staff Engineer")
    pdf = render_pdf(html)
    assert isinstance(pdf, bytes)
    assert pdf.startswith(b"%PDF")


def test_write_pdf_creates_missing_parent_directories(tmp_path):
    target = tmp_path / "variants" / "nested" / "job-1.pdf"
    html = cover_letter_html(CoverLetter(paragraphs=["Hello."]), CONTACT)
    written = write_pdf(html, target)
    assert written == target
    assert target.exists()
    assert target.read_bytes().startswith(b"%PDF")
