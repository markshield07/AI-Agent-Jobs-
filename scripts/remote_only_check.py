# ruff: noqa: E501
"""Read-only: how many sent applications the fully-remote rule would have
skipped, and why. Opens the DB with mode=ro; prints titles, companies and the
rule's reason only.
Run from the job-agent folder:  .\\.venv\\Scripts\\python scripts\\remote_only_check.py [days]
"""

import os
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta

from jobagent.discovery.scoring.rules import remote_only_reason

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
DB = os.path.join("data", "jobagent.db")
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
since = (datetime.now(UTC) - timedelta(days=DAYS)).isoformat()
sent = [
    dict(r)
    for r in conn.execute(
        """SELECT a.submitted_at, a.ats, j.title, j.company, j.location, j.remote, j.description
           FROM applications a JOIN jobs j ON j.id = a.job_id
           WHERE a.submitted_at IS NOT NULL AND a.submitted_at >= ?
           ORDER BY a.submitted_at""",
        (since,),
    ).fetchall()
]
skipped = []
for row in sent:
    if row["remote"] is not None:
        row["remote"] = bool(row["remote"])
    reason = remote_only_reason(row)
    if reason:
        skipped.append((row, reason))

print(f"Sent in the last {DAYS} days: {len(sent)}")
print(f"Would have been skipped by the fully-remote rule: {len(skipped)}")
kinds = Counter(
    "no description on file"
    if "no full description" in r
    else "never says fully remote"
    if "nothing in the posting" in r
    else "board marks it on-site"
    if "board marks" in r
    else "says hybrid / on-site / relocation / a place to live"
    for _, r in skipped
)
for kind, n in kinds.most_common():
    print(f"  {n:4}  {kind}")
print()
for row, reason in skipped:
    print(
        f"{str(row['submitted_at'])[:10]}  {row['ats']:<10} {row['title']} | {row['company']} | {row['location']}"
    )
    print(f"            {reason}")
