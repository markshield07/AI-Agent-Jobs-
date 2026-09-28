"""A validated plan on the page: ATS-safe HTML, plain text and a PDF.

The plan says which facts to show and how each bullet reads; everything else
on the page (employer, title, dates, the skill and education lines) is copied
from the facts, so the renderer cannot say anything the validator did not
check. HTML and plain text are built from one view of the plan, so the text
the coverage gate measures is what the PDF says, save for the target title
(the role applied for), which the caller supplies to each call separately.

ATS-safe means one column, no tables, images or floats, system fonts and plain
section headings: the parsers on the other side read text in document order
and lose anything laid out any other way. Styling lives in a <style> block in
each template; WeasyPrint turns that into the PDF (blueprint: "styling in CSS
beats ReportLab's drawing API").
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from jobagent.resume.facts import Fact
from jobagent.tailor.models import CoverLetter, Entry, TailoredResume

_TEMPLATES = Path(__file__).parent / "templates"

# Never mark anything safe: fact text, contact details and the model's bullets
# are all user data as far as the page is concerned.
_env = Environment(
    loader=FileSystemLoader(_TEMPLATES),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

# Section headings, in page order. Plain words: ATS parsers key on these.
SUMMARY = "Summary"
SKILLS = "Skills"
EXPERIENCE = "Experience"
PROJECTS = "Projects"
EDUCATION = "Education"
CERTIFICATIONS = "Certifications"


@dataclass(slots=True)
class _EntryView:
    heading: str  # "title — employer", whichever exist
    dates: str  # "start – end", whichever exist; "" for no date line
    bullets: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.heading or self.dates or self.bullets)


@dataclass(slots=True)
class _ResumeView:
    name: str
    contact_line: str
    target_title: str
    summary: str
    skills: list[str]
    experience: list[_EntryView]
    projects: list[_EntryView]
    education: list[str]
    credentials: list[str]

    @property
    def title(self) -> str:
        return f"{self.name} – Resume" if self.name else "Resume"


# ---------------------------------------------------------------- public --


def resume_html(
    plan: TailoredResume,
    facts: Mapping[int, Fact],
    contact: Mapping[str, Any],
    *,
    target_title: str | None = None,
) -> str:
    """The tailored resume as a self-contained, single-column HTML page."""
    view = _build_resume(plan, facts, contact, target_title)
    return _env.get_template("resume.html").render(resume=view)


def resume_text(
    plan: TailoredResume,
    facts: Mapping[int, Fact],
    contact: Mapping[str, Any],
    *,
    target_title: str | None = None,
) -> str:
    """The same content as `resume_html`, in the same order, as plain text."""
    view = _build_resume(plan, facts, contact, target_title)
    return "\n".join(_text_lines(view)).rstrip() + "\n"


def cover_letter_html(
    letter: CoverLetter,
    contact: Mapping[str, Any],
    *,
    company: str | None = None,
    job_title: str | None = None,
) -> str:
    """The letter as an HTML page: header, an optional "Re:" line, the paragraphs.

    No date: the caller decides when the letter is sent.
    """
    name = _name(contact)
    return _env.get_template("cover_letter.html").render(
        title=f"{name} – Cover letter" if name else "Cover letter",
        name=name,
        contact_line=_contact_line(contact),
        reference=_reference_line(company, job_title),
        paragraphs=_present(*letter.paragraphs),
    )


def render_pdf(html: str) -> bytes:
    """Lay `html` out on Letter pages and return the PDF bytes."""
    # Imported here, not at module level: WeasyPrint loads its native text stack
    # on import, which is slow and needs libraries the HTML and text paths do not.
    from weasyprint import HTML

    return bytes(HTML(string=html).write_pdf())


def write_pdf(html: str, path: Path) -> Path:
    """Render `html` to a PDF at `path`, creating parent directories as needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_pdf(html))
    return path


# ------------------------------------------------------------------ view --


def _build_resume(
    plan: TailoredResume,
    facts: Mapping[int, Fact],
    contact: Mapping[str, Any],
    target_title: str | None,
) -> _ResumeView:
    return _ResumeView(
        name=_name(contact),
        contact_line=_contact_line(contact),
        target_title=_text(target_title),
        summary=_text(plan.summary),
        skills=_present(*(f.text for f in _lookup(plan.skills, facts))),
        experience=_entry_views(plan.experience, facts),
        projects=_entry_views(plan.projects, facts),
        education=_fact_lines(_lookup(plan.education, facts)),
        credentials=_fact_lines(_lookup(plan.credentials, facts)),
    )


def _lookup(ids: list[int], facts: Mapping[int, Fact]) -> list[Fact]:
    """The facts for `ids`, in order, once each.

    An id the mapping lacks renders nothing: the validator has already refused
    a plan that cites an unknown fact, and a renderer that raised instead would
    make a stored variant impossible to view.
    """
    seen: set[int] = set()
    found: list[Fact] = []
    for fid in ids:
        fact = facts.get(fid)
        if fact is not None and fid not in seen:
            seen.add(fid)
            found.append(fact)
    return found


def _entry_views(entries: list[Entry], facts: Mapping[int, Fact]) -> list[_EntryView]:
    """The entries with something on the page.

    One with neither heading nor bullets is dropped here rather than in the
    templates so the section heading above it disappears with it in HTML and
    text alike.
    """
    return [view for view in (_entry_view(e, facts) for e in entries) if view]


def _entry_view(entry: Entry, facts: Mapping[int, Fact]) -> _EntryView:
    head = facts.get(entry.fact_ids[0]) if entry.fact_ids else None
    detail = _detail(head) if head else {}
    return _EntryView(
        heading=" — ".join(_present(detail.get("title"), detail.get("employer"))),
        dates=_date_span(detail),
        bullets=_present(*(b.text for b in entry.bullets)),
    )


def _fact_lines(facts: list[Fact]) -> list[str]:
    return [line for line in map(_fact_line, facts) if line]


def _fact_line(fact: Fact) -> str:
    """An education or credential line: the text, then the institution and dates."""
    detail = _detail(fact)
    line = " — ".join(_present(fact.text, detail.get("employer")))
    span = _date_span(detail)
    return f"{line} ({span})" if line and span else line or span


def _date_span(detail: Mapping[str, Any]) -> str:
    return " – ".join(_present(detail.get("start"), detail.get("end")))


def _detail(fact: Fact) -> dict[str, Any]:
    """The detail dict, or nothing: a user-added fact may carry any JSON here."""
    return fact.detail if isinstance(fact.detail, dict) else {}


# ---------------------------------------------------------------- header --


def _name(contact: Mapping[str, Any]) -> str:
    return _text(contact.get("full_name"))


def _contact_line(contact: Mapping[str, Any]) -> str:
    links = contact.get("links")
    if not isinstance(links, list | tuple):
        links = [links]
    parts = [contact.get("email"), contact.get("phone"), contact.get("location"), *links]
    return " | ".join(_present(*parts))


def _reference_line(company: str | None, job_title: str | None) -> str:
    company, job_title = _text(company), _text(job_title)
    if company and job_title:
        return f"Re: {job_title} at {company}"
    subject = job_title or company
    return f"Re: {subject}" if subject else ""


# ------------------------------------------------------------ plain text --


def _text_lines(view: _ResumeView) -> list[str]:
    lines = _present(view.name, view.contact_line, view.target_title)
    sections: list[list[str]] = [
        _section(SUMMARY, [view.summary] if view.summary else []),
        _section(SKILLS, [", ".join(view.skills)] if view.skills else []),
        _section(EXPERIENCE, _entries_text(view.experience)),
        _section(PROJECTS, _entries_text(view.projects)),
        _section(EDUCATION, view.education),
        _section(CERTIFICATIONS, view.credentials),
    ]
    for body in sections:
        if body:
            if lines:
                lines.append("")
            lines += body
    return lines


def _section(heading: str, body: list[str]) -> list[str]:
    return [heading, *body] if body else []


def _entries_text(entries: list[_EntryView]) -> list[str]:
    lines: list[str] = []
    for i, entry in enumerate(entries):
        if i:
            lines.append("")
        lines += _present(entry.heading, entry.dates)
        lines += [f"- {bullet}" for bullet in entry.bullets]
    return lines


# --------------------------------------------------------------- helpers --


def _text(value: Any) -> str:
    """One line of page-safe text.

    None and containers render nothing (a user-added fact may carry any JSON in
    its detail, and a dict's repr does not belong on a resume). Anything else is
    str()-ed with its whitespace collapsed, so a newline inside a bullet, a
    skill or a name cannot split a plain-text line or the one-line Skills
    section.
    """
    if value is None or _is_container(value):
        return ""
    return " ".join(str(value).split())


def _is_container(value: Any) -> bool:
    return isinstance(value, Mapping | Sequence | Set) and not isinstance(value, str)


def _present(*values: Any) -> list[str]:
    """The non-empty values, as one-line strings, in order."""
    return [text for text in (_text(v) for v in values) if text]
