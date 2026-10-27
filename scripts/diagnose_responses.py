# ruff: noqa: E501, E731, E741
"""Read-only look at why sent applications get no interviews. Opens the DB with
mode=ro and prints counts only: no mail bodies, no passwords.
Run from the job-agent folder:  .\\.venv\\Scripts\\python scripts\\diagnose_responses.py
"""

import json
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime

DB = os.path.join("data", "jobagent.db")
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
q = lambda sql, *a: [dict(r) for r in conn.execute(sql, a).fetchall()]


def show(title, rows):
    print(f"\n== {title}")
    for r in rows if isinstance(rows, list) else [rows]:
        print("  ", r)


def day(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


sent = q(
    """SELECT a.id, a.ats, a.submitted_at, a.variant_id, j.title, j.company, j.location,
              j.remote, j.salary_min, j.salary_max, j.posted_at, j.scraped_at, j.source,
              j.score, j.tier, j.fit_score, s.status
       FROM applications a JOIN jobs j ON j.id = a.job_id
       JOIN application_status s ON s.application_id = a.id
       WHERE a.submitted_at IS NOT NULL ORDER BY a.submitted_at"""
)
show("sent", len(sent))
show("sent by site", Counter(r["ats"] for r in sent).most_common())
show("sent by week", sorted(Counter(day(r["submitted_at"]).strftime("%G-W%V") for r in sent).items()))
show("status now", Counter(r["status"] for r in sent).most_common())
show("remote flag", Counter(r["remote"] for r in sent).most_common())
show(
    "not remote: locations",
    Counter(r["location"] for r in sent if r["remote"] != 1).most_common(40),
)
show("titles", Counter(r["title"] for r in sent).most_common(60))
show("companies sent to more than once", [(c, n) for c, n in Counter(r["company"] for r in sent).items() if n > 1])
agency = re.compile(r"staff|recruit|talent|consult|search|solutions|resourc|group|partners|tek|infotech|systems", re.I)
show("looks like staffing/consulting", sum(1 for r in sent if agency.search(r["company"] or "")))

ages = []
for r in sent:
    p, s = day(r["posted_at"]), day(r["submitted_at"])
    if p and s:
        ages.append(round((s.replace(tzinfo=None) - p.replace(tzinfo=None)).total_seconds() / 86400, 1))
ages.sort()
if ages:
    show("posting age at send (days): min/median/max, share over 3 days", [ages[0], ages[len(ages) // 2], ages[-1], round(sum(a > 3 for a in ages) / len(ages), 2)])

paid = [r for r in sent if r["salary_max"]]
show("sent with a posted pay range", len(paid))
show("posted max under 160000", sum(1 for r in paid if r["salary_max"] < 160000))
show("posted ranges", sorted((r["salary_min"], r["salary_max"]) for r in paid))
show("rules score / tier / fit", [(r["score"], r["tier"], r["fit_score"]) for r in sent][:200])

# What went on the forms that were sent: the questions that screen people out.
screen = re.compile(
    r"degree|bachelor|education|years|experience|salary|compensation|pay|clearance|sponsor|authori[sz]|relocat|on.?site|hybrid|commute|certif|ccnp|ccie|ccna|license|travel|shift|on.?call|weekend",
    re.I,
)
answers = Counter()
model_answers = Counter()
for row in q(
    """SELECT t.filled FROM submission_attempts t JOIN applications a ON a.id = t.application_id
       WHERE t.outcome = 'submitted' AND t.filled IS NOT NULL"""
):
    for f in json.loads(row["filled"] or "[]"):
        label = (f.get("label") or f.get("key") or "")[:110]
        value = f.get("value")
        if f.get("file_path") or not screen.search(label):
            continue
        value = json.dumps(value)[:60]
        answers[(label, value, f.get("source"))] += 1
        if f.get("source") == "model":
            model_answers[(label, value)] += 1
show("screening answers sent (label, value, source), most common", answers.most_common(120))
show("of those, written by the model", model_answers.most_common(60))

v = q(
    """SELECT rv.keyword_coverage kc, rv.base_coverage bc, rv.pdf_path, rv.created_at
       FROM applications a JOIN resume_variants rv ON rv.id = a.variant_id
       WHERE a.submitted_at IS NOT NULL"""
)
if v:
    kc = sorted(r["kc"] or 0 for r in v)
    show("tailored coverage of posting keywords: min/median/max", [kc[0], kc[len(kc) // 2], kc[-1]])
    show("tailored minus upload coverage, median", sorted((r["kc"] or 0) - (r["bc"] or 0) for r in v)[len(v) // 2])
    show("uploaded file names (sample)", [os.path.basename(r["pdf_path"] or "") for r in v[:5]])

show("cover letters sent", q("SELECT COUNT(*) n FROM applications WHERE submitted_at IS NOT NULL AND cover_letter_path IS NOT NULL"))

show("inbox rows", q("SELECT COUNT(*) n, MIN(received_at) first, MAX(received_at) last FROM inbox_messages"))
show("inbox by label and matched", q("SELECT label, application_id IS NOT NULL matched, COUNT(*) n FROM inbox_messages GROUP BY 1,2 ORDER BY 1,2"))
show("status events from mail", q("SELECT to_status, source, COUNT(*) n FROM application_events WHERE to_status IS NOT NULL GROUP BY 1,2"))
hiring = re.compile(r"applica|interview|candida|position|role|opportunit|recruit|resume|hiring|thank you for|update|next step|schedule|phone|call|chat|viewed", re.I)
show(
    "unmatched mail that reads like hiring (date, sender domain, subject)",
    [
        (r["received_at"][:10], (r["from_addr"] or "").split("@")[-1], (r["subject"] or "")[:90], r["label"])
        for r in q("SELECT received_at, from_addr, subject, label FROM inbox_messages WHERE application_id IS NULL ORDER BY received_at")
        if hiring.search(r["subject"] or "")
    ][:150],
)
show(
    "matched mail (date, sender domain, subject, label)",
    [
        (r["received_at"][:10], (r["from_addr"] or "").split("@")[-1], (r["subject"] or "")[:90], r["label"])
        for r in q("SELECT received_at, from_addr, subject, label FROM inbox_messages WHERE application_id IS NOT NULL ORDER BY received_at")
    ],
)
show("LinkedIn 'viewed' mails", q("SELECT COUNT(*) n FROM inbox_messages WHERE subject LIKE '%viewed%'"))
