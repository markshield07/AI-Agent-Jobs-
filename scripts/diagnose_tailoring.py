"""Why tailored resumes get rejected: a read-only report over the local database.

    .venv/bin/python scripts/diagnose_tailoring.py [--limit 60] [--db data/jobagent.db]

For the newest rejected variants it says which check sank each one, and for a
coverage rejection which keywords the uploaded resume had that the tailored one
lacked, split into those a fact states (the plan left them out) and those no
fact states (no plan could bring them back). It also counts the facts by kind,
the bullets per role, and how often the same job was tailored again. Nothing is
written to the database.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

from jobagent.answers import list_answers
from jobagent.resume.facts import list_facts
from jobagent.tailor.keywords import coverage
from jobagent.tailor.models import TailoredResume
from jobagent.tailor.pipeline import CONTACT_KEYS
from jobagent.tailor.render import resume_text
from jobagent.tailor.terms import contains_term
from jobagent.tailor.validate import fact_pool_text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="data/jobagent.db")
    parser.add_argument("--limit", type=int, default=60)
    args = parser.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"No database at {path}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    facts = list_facts(conn)
    by_id = {f.id: f for f in list_facts(conn, include_inactive=True) if f.id is not None}
    pool = fact_pool_text(facts)
    text_pool = "\n".join(f.text for f in facts)
    contact = {r["key"]: r["value"] for r in list_answers(conn) if r["key"] in CONTACT_KEYS}
    base = conn.execute("SELECT parsed_text FROM resume_base ORDER BY id DESC LIMIT 1").fetchone()
    base_text = base["parsed_text"] if base else ""

    print("== Facts ==")
    print("active by kind:", dict(Counter(f.kind for f in facts)))
    per_role: dict[str, int] = defaultdict(int)
    for f in facts:
        if f.kind == "role":
            d = f.detail if isinstance(f.detail, dict) else {}
            per_role[f"{d.get('title') or '?'} @ {d.get('employer') or '?'}"] += 1
    for role, n in sorted(per_role.items(), key=lambda kv: -kv[1]):
        print(f"  {n:3d} role facts  {role}")
    print(f"uploaded resume: {len(base_text)} chars")

    print("\n== Variants ==")
    counts = conn.execute(
        """SELECT status, COUNT(*) n, COUNT(DISTINCT job_id) jobs
           FROM resume_variants GROUP BY status"""
    ).fetchall()
    for r in counts:
        print(f"  {r['status']}: {r['n']} variants over {r['jobs']} jobs")
    again = conn.execute(
        """SELECT job_id, COUNT(*) n FROM resume_variants WHERE status = 'rejected'
           GROUP BY job_id HAVING n > 1 ORDER BY n DESC LIMIT 10"""
    ).fetchall()
    if again:
        repeats = ", ".join(f"{r['job_id']}x{r['n']}" for r in again)
        print("  jobs tailored again and again:", repeats)

    rows = conn.execute(
        """SELECT v.*, j.title, j.company FROM resume_variants v LEFT JOIN jobs j ON j.id = v.job_id
           WHERE v.status = 'rejected' ORDER BY v.id DESC LIMIT ?""",
        (args.limit,),
    ).fetchall()
    seen_jobs: set[str] = set()
    sank: Counter[str] = Counter()
    lost_stated: Counter[str] = Counter()
    lost_unstated: Counter[str] = Counter()
    lost_tag_only: Counter[str] = Counter()
    never_on_either: Counter[str] = Counter()
    gaps: list[float] = []
    print(f"\n== Newest rejected variants (one per job, up to {args.limit} rows) ==")
    for r in rows:
        if r["job_id"] in seen_jobs:
            continue
        seen_jobs.add(r["job_id"])
        issues = json.loads(r["issues"] or "[]")
        wheres = sorted({(i.get("where") or "?").split("[")[0].split(".")[0] for i in issues})
        sank.update(["coverage" if wheres == ["coverage"] else "fact check"])
        keywords = json.loads(r["keywords"] or "[]")
        plan = TailoredResume.model_validate_json(r["content"]) if r["content"] else None
        text = resume_text(plan, by_id, contact) if plan else ""
        cov = coverage(text, keywords)
        base_cov = r["base_coverage"]
        if base_cov is not None:
            gaps.append(base_cov - cov)
        lost = [k for k in keywords if contains_term(base_text, k) and not contains_term(text, k)]
        for k in lost:
            if contains_term(text_pool, k):
                lost_stated[k] += 1
            elif contains_term(pool, k):
                lost_tag_only[k] += 1
            else:
                lost_unstated[k] += 1
        for k in keywords:
            if not contains_term(base_text, k) and not contains_term(pool, k):
                never_on_either[k] += 1
        print(
            f"- {r['title']} @ {r['company']}: tailored {cov:.0%} vs uploaded "
            f"{(base_cov or 0):.0%}, {len(keywords)} keywords, {r['attempts']} attempts, "
            f"sunk by {', '.join(wheres) or '?'}"
        )
        if len(seen_jobs) <= 5:
            print(f"    keywords: {', '.join(keywords)}")
        if lost:
            print(f"    uploaded had, tailored lacks: {', '.join(lost[:15])}")
        bullets = [len(e.bullets) for e in plan.experience] if plan else []
        print(f"    entries {len(bullets)}, bullets per entry {bullets}")
        for i in issues[:3]:
            print(f"    issue {i.get('where')}: {(i.get('message') or '')[:160]}")

    def top(c: Counter[str]) -> str:
        return ", ".join(f"{k}({n})" for k, n in c.most_common(25)) or "(none)"

    print("\n== Summary ==")
    print("sunk by (one per job):", dict(sank))
    every = Counter(
        "coverage"
        if {(i.get("where") or "") for i in json.loads(r["issues"] or "[]")} == {"coverage"}
        else "fact check"
        for r in rows
    )
    print(f"sunk by (all {len(rows)} rows):", dict(every))
    if gaps:
        print(f"average gap uploaded minus tailored: {sum(gaps) / len(gaps):.1%}")
    print("lost, a fact's text states it:", top(lost_stated))
    print("lost, only a fact's tags or details state it:", top(lost_tag_only))
    print("lost, no fact states it (uploaded resume only):", top(lost_unstated))
    print("keywords on neither resume nor facts:", top(never_on_either))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
