# ruff: noqa: E501, E731, E741
"""Read-only audit of the live job agent. Opens the DB with mode=ro, only GETs the dashboard.
Run from the job-agent folder:  .\\.venv\\Scripts\\python scripts\\audit_readonly.py
"""

import json
import os
import sqlite3
import subprocess
import urllib.request
from datetime import UTC, datetime, timedelta

DB = os.path.join("data", "jobagent.db")
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
q = lambda sql, *a: [dict(r) for r in conn.execute(sql, a).fetchall()]


def show(title, rows):
    print(f"\n== {title}")
    for r in rows if isinstance(rows, list) else [rows]:
        print("  ", r)


def sh(cmd):
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60
        ).stdout.strip()
    except Exception as e:
        return f"ERR {e}"


show(
    "git",
    [
        sh(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        sh(["git", "log", "--oneline", "-3"]),
        sh(["git", "status", "--short"]),
    ],
)
env = {}
if os.path.exists(".env"):
    for line in open(".env", encoding="utf-8", errors="replace"):
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.strip().split("=", 1)
            env[k] = (
                "<set>"
                if any(s in k.upper() for s in ("PASSWORD", "KEY", "TOKEN", "SECRET"))
                else v
            )
show(".env (secrets masked)", env)
show(
    "JOBAGENT_ process env",
    {k: v for k, v in os.environ.items() if k.startswith("JOBAGENT_") and "PASSWORD" not in k},
)
show("schema_version", q("SELECT * FROM schema_version ORDER BY version DESC LIMIT 3"))
show(
    "lock file",
    os.path.exists(os.path.join("data", "apply.lock"))
    and open(os.path.join("data", "apply.lock"), encoding="utf-8", errors="replace").read()[:200],
)

show(
    "jobs by source/status",
    q("SELECT source, status, COUNT(*) n FROM jobs GROUP BY 1,2 ORDER BY 1,2"),
)
show(
    "jobs by category",
    q(
        "SELECT COALESCE(NULLIF(TRIM(category),''),'Uncategorised') c, COUNT(*) n FROM jobs GROUP BY 1 ORDER BY 2 DESC"
    ),
)
show("application status", q("SELECT status, COUNT(*) n FROM application_status GROUP BY 1"))
show(
    "applications by ats/mode/submitted",
    q(
        "SELECT ats, mode, submitted_at IS NOT NULL sent, COUNT(*) n FROM applications GROUP BY 1,2,3"
    ),
)
show(
    "attempt outcomes by handler",
    q("SELECT handler, outcome, COUNT(*) n FROM submission_attempts GROUP BY 1,2 ORDER BY 1,2"),
)
show(
    "submitted per Pacific day per site (UTC-7)",
    q(
        "SELECT date(submitted_at, '-7 hours') d, ats, COUNT(*) n FROM applications WHERE submitted_at IS NOT NULL GROUP BY 1,2 ORDER BY 1 DESC LIMIT 30"
    ),
)
now = datetime.now(UTC)
since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
show(
    "submitted trailing 24h per site (what the caps count)",
    q(
        "SELECT ats, COUNT(*) n, MIN(submitted_at) first, MAX(submitted_at) last FROM applications WHERE submitted_at >= ? GROUP BY 1",
        since,
    ),
)
show(
    "timestamp formats (sample)",
    q(
        "SELECT submitted_at, created_at FROM applications WHERE submitted_at IS NOT NULL ORDER BY id DESC LIMIT 3"
    ),
)
show(
    "all submitted applications",
    q(
        """SELECT a.id, a.ats, a.submitted_at, j.title, j.company, j.location, s.status,
              (SELECT substr(confirmation,1,80) FROM submission_attempts t WHERE t.application_id=a.id AND t.outcome='submitted' ORDER BY t.id DESC LIMIT 1) conf,
              (SELECT COUNT(*) FROM submission_attempts t WHERE t.application_id=a.id AND t.outcome='submitted') n_submit_attempts
       FROM applications a JOIN jobs j ON j.id=a.job_id JOIN application_status s ON s.application_id=a.id
       WHERE a.submitted_at IS NOT NULL ORDER BY a.submitted_at"""
    ),
)
show(
    "possible duplicate submissions (same company+title)",
    q(
        """SELECT lower(j.company) co, lower(j.title) t, COUNT(*) n, group_concat(a.ats) sites FROM applications a JOIN jobs j ON j.id=a.job_id
       WHERE a.submitted_at IS NOT NULL GROUP BY 1,2 HAVING n>1"""
    ),
)
show(
    "jobs marked applied without a submitted application",
    q(
        "SELECT j.id, j.title, j.company FROM jobs j LEFT JOIN applications a ON a.job_id=j.id WHERE j.status='applied' AND (a.submitted_at IS NULL)"
    ),
)
show(
    "submitted applications whose job is not marked applied",
    q(
        "SELECT j.id, j.status, j.title FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.submitted_at IS NOT NULL AND j.status<>'applied'"
    ),
)
show(
    "unconfirmed/blocked/failed/needs_input (newest 25)",
    q(
        """SELECT a.id, a.ats, s.status, j.title, j.company,
              (SELECT substr(COALESCE(error,''),1,160) FROM submission_attempts t WHERE t.application_id=a.id ORDER BY t.id DESC LIMIT 1) err
       FROM applications a JOIN jobs j ON j.id=a.job_id JOIN application_status s ON s.application_id=a.id
       WHERE s.status IN ('unconfirmed','blocked','failed','needs_input') ORDER BY a.id DESC LIMIT 25"""
    ),
)
show(
    "answers (key = value)",
    q("SELECT key, substr(value,1,80) value, confidence, updated_at FROM answers ORDER BY key"),
)
show("variants", q("SELECT status, COUNT(*) n FROM resume_variants GROUP BY 1"))
show("facts", q("SELECT kind, active, COUNT(*) n FROM resume_facts GROUP BY 1,2"))
show("runs (last 10)", q("SELECT * FROM runs ORDER BY id DESC LIMIT 10"))
show(
    "events by kind/to_status",
    q("SELECT kind, to_status, source, COUNT(*) n FROM application_events GROUP BY 1,2,3"),
)
show("inbox messages", q("SELECT COUNT(*) n FROM inbox_messages"))


def get(path):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}


stats = get("/api/stats?days=30&tz_offset_minutes=-420")
show("API /api/config", get("/api/config"))
show("API /api/stats totals", stats.get("totals"))
show("API /api/stats window_totals", stats.get("window_totals"))
show("API /api/stats last 7 days", (stats.get("per_day") or [])[-7:])
show("API /api/stats by_site", stats.get("by_site"))
show("API /api/stats by_category", stats.get("by_category"))
show("API /api/stats funnel", stats.get("funnel"))
show("API /api/stats attention", stats.get("attention"))
show("API /api/sessions", get("/api/sessions"))
show("API /api/activity", get("/api/activity?limit=8"))
show(
    "scheduled tasks",
    [l for l in sh(["schtasks", "/query", "/fo", "CSV", "/nh"]).splitlines() if "job" in l.lower()],
)

try:
    from playwright.sync_api import sync_playwright

    os.makedirs(os.path.join("data", "audit"), exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1400, "height": 1000})
        errors = []
        pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
        pg.on("pageerror", lambda e: errors.append(str(e)))
        for view in ("overview", "applications", "jobs", "profile", "settings"):
            pg.goto(f"http://127.0.0.1:8000/#{view}")
            pg.wait_for_timeout(2500)
            pg.screenshot(path=os.path.join("data", "audit", f"{view}.png"), full_page=True)
        b.close()
    show("dashboard JS errors", errors or ["none"])
    print("screenshots in data\\audit\\")
except Exception as e:
    show("screenshots skipped", str(e))
