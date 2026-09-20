# Job Agent

An agent that finds jobs across the boards, tailors a resume for each posting
from facts you actually supplied, submits the application, watches for replies,
and reports the lot on a dashboard.

Architecture and the reasoning behind each decision:
**[Auto-Apply Agent Blueprint](https://claude.ai/artifact/CMmq6LQBy6Dd1ibdnbj3y2)**.

## Where this is up to

| Phase | What it covers | State |
|---|---|---|
| 1 | Scaffold, database schema, resume intake, fact base | done |
| 2 | Discovery, dedupe, enrichment, scoring | **this branch** |
| 3 | Tailoring and PDF rendering | not started |
| 4 | Submission | not started |
| 5 | Response tracking | not started |
| 6 | Dashboard and scheduling | not started |

## The fact base

The idea the rest of the project rests on: your resume is parsed into
individually addressable facts — one per role, project, skill, degree — and the
skills and keywords you type in are stored the same way, marked `user_added`.

When phase 3 tailors a resume for a posting, it may select, reorder and rephrase
from that set and nothing else. A claim with no fact behind it never reaches a
PDF. Adding a keyword is you asserting it, not the model deciding it may.

A never-claim list sits alongside it: technologies you have not used. A fact or
keyword naming one is refused at the point you add it, not at render time.

## Running it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
jobagent serve                # http://127.0.0.1:8000
```

### Which model bill

Every model call goes through one seam (`llm/backend.py`) with two backends
behind it, picked by `JOBAGENT_LLM_BACKEND` in `.env`:

| Setting | What it uses | When to pick it |
|---|---|---|
| `claude-code` | The `claude` command line, on your Claude subscription login | You already pay for Claude and the agent runs on a machine where you are logged in |
| `api` | The Claude API with `ANTHROPIC_API_KEY`, metered per token | Headless or scheduled runs on a box with no login, or you want per-run cost figures |
| `auto` (default) | The API if a key is set, otherwise `claude` if it is installed | You do not want to think about it |

For the subscription route: install Claude Code, run `claude auth login`
once, and leave `ANTHROPIC_API_KEY` unset. The agent shells out to
`claude -p` with a JSON schema and never sees your login token. Usage counts
against the subscription's limits, so a large overnight run can hit them.

### Finding jobs

Tell it what to look for, then run a pass:

```bash
jobagent criteria --title "Software Engineer" --title "Backend Engineer" \
    --location Remote --keyword python --keyword aws \
    --board greenhouse:stripe --board lever:netflix:Netflix --site indeed
jobagent discover             # or POST /api/runs/discover from the dashboard
```

A run searches every company board in the criteria (Greenhouse, Lever and
Ashby publish their postings as public JSON with full descriptions) and, for
each title and location pair, the aggregator sites through JobSpy (Indeed,
LinkedIn and the rest, by scraping). What comes back goes through:

1. **Dedupe.** A posting's id is a hash of its cleaned URL, title and company,
   and the URL is unique on its own, so the same job seen on two boards lands
   once and a re-listed one cannot be applied to twice.
2. **Enrichment.** A posting that arrived as a snippet gets its page fetched
   and the full description pulled from JSON-LD, then known selectors per
   platform, then (only with `--enrich-with-model`) the model.
3. **Rules.** A deterministic 0 to 100 score on title, salary, location and
   keyword overlap with your fact base, with hard rejects for excluded terms,
   blacklisted companies and salary below your floor. Free, runs on
   everything, and the reason for each score is stored with the job.
4. **Model tiering.** Rule survivors go to the model in batches against your
   profile, which is the cached prefix of every call. Each comes back with a
   tier (1 strong, 2 good, 3 stretch, 4 not a fit), a fit score, a category
   and a one-line reason. Tier at or under `max_tier` is queued for phase 4;
   the rest is skipped, reason attached.

Each stage reads its input back from the database, so a run that dies half
way strands nothing. Without a usable model backend the run still completes
and the survivors wait, marked pending, for the next one.

Tests and linting:

```bash
pytest
ruff check . && ruff format --check .
```

Parsing a resume costs one model call. Re-uploading a byte-identical file
returns the existing record instead of parsing it again.

## API

| Method | Path | What it does |
|---|---|---|
| `POST` | `/api/resume` | Upload a `.pdf`, `.docx`, `.txt` or `.md` resume and parse it into facts |
| `GET` | `/api/resume` | The resume currently on file |
| `GET` | `/api/facts` | The fact base. Filter by `kind`, `source`, `include_inactive` |
| `POST` | `/api/facts` | Add one fact |
| `POST` | `/api/facts/keywords` | Bulk-add skills or keywords |
| `PATCH` | `/api/facts/{id}` | Deactivate or restore a fact |
| `GET`/`POST`/`DELETE` | `/api/never-claim` | The never-claim list |
| `GET` | `/api/answers` | The answer bank, for form filling in phase 4 |
| `GET`/`PUT` | `/api/search-criteria` | What to search for: titles, locations, keywords, boards, thresholds |
| `GET` | `/api/jobs` | Discovered jobs, best first. Filter by `status`, `min_score`, `source` |
| `GET` | `/api/jobs/counts` | How many jobs in each status |
| `GET`/`PATCH` | `/api/jobs/{id}` | One job; `PATCH` to queue or skip it by hand |
| `GET` | `/api/runs` | Past discovery runs with their counts and token use |
| `POST` | `/api/runs/discover` | Start a run in the background; `?wait=true` returns the report |

Interactive docs at `/docs` while the server is running.

## Layout

```
src/jobagent/
├── config.py           Settings, read from the environment
├── answers.py          Standing answers to application form questions
├── db/
│   ├── schema.sql      Every table, with the two invariants stated at the top
│   └── database.py     Connections (one per thread), WAL, transactions
├── llm/
│   └── backend.py      One seam for model calls: API or Claude Code subscription
├── resume/
│   ├── extract.py      File bytes to plain text
│   ├── parser.py       Text to facts, via the model
│   ├── facts.py        The fact base and the never-claim list
│   ├── profile.py      The fact base as tags and as a short profile, for scoring
│   └── intake.py       Upload, store, parse, record
├── discovery/
│   ├── criteria.py     What to search for, stored as one settings row
│   ├── sources/        Greenhouse, Lever, Ashby (public JSON) and JobSpy (scraping)
│   ├── enrich.py       Snippet to full description: JSON-LD, selectors, model
│   ├── scoring/
│   │   ├── rules.py    The free 0 to 100 pass, with reasons
│   │   └── classify.py The batched model pass that tiers the survivors
│   ├── store.py        Jobs and runs on disk; dedupe on the way in
│   └── pipeline.py     One run, stage by stage
├── api/                FastAPI routes
└── main.py             App factory and the command line
```

Two invariants the schema enforces and the rest of the code assumes:

- **`application_events` is append-only.** An application's status is derived
  from its newest event by the `application_status` view, never written in
  place. Response rate and time-to-first-reply are computed from the gaps
  between events, and a status column overwrites that history.
- **`resume_facts` is the only source a tailored resume may draw from.**

## Prior art

Five references informed the design, cited in the blueprint where each idea came
from: [AutoApply](https://github.com/AbhishekMandapmalvi/AutoApply),
[jobpilot](https://github.com/suxrobGM/jobpilot),
[job-pilot](https://github.com/mubashirali/job-pilot),
[AIApplyJobs](https://github.com/vivekshekharrai-del/AIApplyJobs),
[job-agent](https://github.com/lordvacuum/job-agent).

## Before you turn on phase 4

Automating submissions to LinkedIn, Indeed, Workday and the ATS vendors may
violate those platforms' terms of service, and can get your accounts
rate-limited or banned. Every one of the five references above carries a version
of this warning. The design leans on public ATS boards first and keeps a
submission delay and a daily cap on by default, but the risk to your accounts is
real and it does not engineer away.
