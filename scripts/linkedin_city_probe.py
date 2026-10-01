"""What LinkedIn's city box does on one job: a dry run that saves the page.

Run on the machine that runs the agent, with the hourly loop stopped:

    .venv/bin/python scripts/linkedin_city_probe.py <job id>

It runs the usual dry run for the job (fills the form, never presses Submit,
discards the draft) and saves the Easy Apply window as HTML at each moment
that matters for the city box, under data/screenshots/:

    <job>-city-1-before.html   the step as it opened, before the box is touched
    <job>-city-2-typed.html    after typing, with LinkedIn's list of places open
    <job>-city-3-picked.html   after the pick
    <job>-city-4-review.html   the review page, when it is the one that stops

and prints what the box held at each moment and which places LinkedIn offered.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from jobagent.apply import pipeline
from jobagent.apply.handlers import linkedin
from jobagent.config import get_settings
from jobagent.db.database import open_database

_WINDOW_HTML = """() => {
  const header = document.querySelector('#dialog-header, .jobs-easy-apply-modal, [role=dialog]');
  const box = header
    ? header.closest('dialog, [role=dialog], .artdeco-modal') || header.parentElement
    : null;
  return (box || document.body).outerHTML;
}"""


def install(job_id: str, out: Path) -> None:
    """Wrap the city pick and the review check so each saves the window."""
    out.mkdir(parents=True, exist_ok=True)

    def save(page, name: str) -> None:
        try:
            html = page.evaluate(_WINDOW_HTML)
        except Exception as exc:
            print(f"could not read the page for {name}: {exc}")
            return
        path = Path(out) / f"{job_id}-city-{name}.html"
        path.write_text(str(html), encoding="utf-8")
        print(f"saved {path}")

    def value(page, field) -> str:
        try:
            return page.locator(field.selector).first.input_value(timeout=1000)
        except Exception as exc:
            return f"(unreadable: {type(exc).__name__})"

    real_typeahead = linkedin._typeahead

    def typeahead(page, field, wanted):
        print(f"city box {field.selector!r} ({field.label!r}) holds {value(page, field)!r}")
        save(page, "1-before")
        box = page.locator(field.selector).first
        real_press = box.__class__.press_sequentially

        def press_then_save(self, *args, **kwargs):
            result = real_press(self, *args, **kwargs)
            page.wait_for_timeout(2500)
            options = page.locator("[role='option']")
            print("places offered:", [t.strip() for t in options.all_inner_texts()][:8])
            save(page, "2-typed")
            return result

        box.__class__.press_sequentially = press_then_save
        try:
            error = real_typeahead(page, field, wanted)
        finally:
            box.__class__.press_sequentially = real_press
        print(f"after the pick the box holds {value(page, field)!r}; error: {error!r}")
        save(page, "3-picked")
        return error

    real_review = linkedin.LinkedInHandler.review_problem

    def review_problem(self, page, root):
        problem = real_review(self, page, root)
        if problem:
            save(page, "4-review")
        return problem

    linkedin._typeahead = typeahead
    linkedin.LinkedInHandler.review_problem = review_problem


def main(job_id: str) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    install(job_id, settings.screenshots_dir)
    db = open_database(settings.db_path)
    try:
        report = pipeline.run_apply(db.connection(), settings, job_ids=[job_id], mode="dry_run")
    finally:
        db.close()
    print(report.summary())
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
