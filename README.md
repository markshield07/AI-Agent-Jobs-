# Job Agent

An agent that finds jobs across the boards, tailors a resume for each posting
from facts you actually supplied, submits the application, watches for replies,
and reports the lot on a dashboard.

Architecture and the reasoning behind each decision:
**[Auto-Apply Agent Blueprint](https://claude.ai/artifact/CMmq6LQBy6Dd1ibdnbj3y2)**.

## Where this is up to

| Phase | What it covers | State |
|---|---|---|
| 1 | Scaffold, database schema, resume intake, fact base | **this branch** |
| 2 | Discovery, dedupe, enrichment, scoring | not started |
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
cp .env.example .env          # add your ANTHROPIC_API_KEY
jobagent                      # http://127.0.0.1:8000
```

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

Interactive docs at `/docs` while the server is running.

## Layout

```
src/jobagent/
├── config.py           Settings, read from the environment
├── answers.py          Standing answers to application form questions
├── db/
│   ├── schema.sql      Every table, with the two invariants stated at the top
│   └── database.py     Connections (one per thread), WAL, transactions
├── resume/
│   ├── extract.py      File bytes to plain text
│   ├── parser.py       Text to facts, via the model
│   ├── facts.py        The fact base and the never-claim list
│   └── intake.py       Upload, store, parse, record
├── api/                FastAPI routes
└── main.py             App factory
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
