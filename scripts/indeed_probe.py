"""Which Indeed search finds jobs that apply on Indeed, for one title and place.

Run on the machine that runs the agent (Indeed does not answer the sandbox):

    python scripts/indeed_probe.py "Network Engineer" "Riverside, CA"
    python scripts/indeed_probe.py "Network Engineer" Remote

For each way of asking Indeed it prints how many postings came back, how many
titles match, how many have no company link (so apply on Indeed), and the
first few titles. Read-only: it only searches.
"""

from __future__ import annotations

import sys

from jobspy import scrape_jobs


def main(title: str, place: str) -> None:
    remote = place.lower() == "remote"
    words = set(title.lower().split())
    base = {"site_name": ["indeed"], "results_wanted": 30, "country_indeed": "USA"}
    where = {"is_remote": True} if remote else {"location": place}
    ways = {
        "usual, 72h": {**base, **where, "search_term": title, "hours_old": None if remote else 72},
        "Indeed Apply": {
            **base,
            "search_term": title,
            "easy_apply": True,
            "location": "Remote" if remote else place,
        },
        "Indeed Apply, quoted": {
            **base,
            "search_term": f'"{title}"',
            "easy_apply": True,
            "location": "Remote" if remote else place,
        },
    }
    for name, call in ways.items():
        try:
            frame = scrape_jobs(**call)
        except Exception as exc:
            print(f"{name}: failed: {type(exc).__name__}: {exc}")
            continue
        rows = frame.to_dict("records") if len(frame) else []
        match = [r for r in rows if words <= set(str(r.get("title") or "").lower().split())]
        on_indeed = [r for r in rows if not isinstance(r.get("job_url_direct"), str)]
        both = [r for r in match if r in on_indeed]
        print(
            f"{name}: {len(rows)} found, {len(match)} titles match, {len(on_indeed)} apply "
            f"on Indeed, {len(both)} both"
        )
        for r in rows[:8]:
            flag = "indeed" if r in on_indeed else "company"
            print(f"    [{flag}] {r.get('title')} | {r.get('company')} | {r.get('job_url')}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
