-- Schema for the job application agent.
--
-- Two rules shape this file:
--   1. `application_events` is append-only. An application's status is derived
--      from its newest event, never written in place, because response rate and
--      time-to-first-reply are computed from the gaps between events.
--   2. `resume_facts` is the only source a tailored resume may draw from. A fact
--      the user typed in is as legitimate as one parsed out of their resume, but
--      a claim with no fact behind it never reaches a PDF.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER NOT NULL,
    applied_at TEXT    NOT NULL
);

-- ------------------------------------------------------- settings and runs --

-- Small JSON documents the dashboard edits in place: search criteria, later
-- schedule and apply-mode.
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- One row per discovery run; the dashboard's run feed and cost tiles read this.
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    found        INTEGER NOT NULL DEFAULT 0,
    scored       INTEGER NOT NULL DEFAULT 0,
    applied      INTEGER NOT NULL DEFAULT 0,
    failed       INTEGER NOT NULL DEFAULT 0,
    tokens_in    INTEGER NOT NULL DEFAULT 0,
    tokens_out   INTEGER NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------- discovery --

CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,          -- sha256(canonical url + title + company)[:16]
    url           TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    company       TEXT NOT NULL,
    source        TEXT NOT NULL,             -- greenhouse | lever | ashby | indeed | linkedin | ...
    location      TEXT,
    description   TEXT,
    ats_type      TEXT,
    apply_url     TEXT,
    salary_min    INTEGER,
    salary_max    INTEGER,
    posted_at     TEXT,
    remote        INTEGER,                   -- 1/0, NULL when the source did not say
    external_id   TEXT,                      -- the source's own id, when it has one
    score         INTEGER,                   -- 0-100 from the rules pass
    score_reason  TEXT,
    tier          INTEGER,                   -- 1-4 from the model pass; NULL if rules rejected it
    fit_score     INTEGER,                   -- 0-100 from the model pass
    category      TEXT,                      -- e.g. "Backend Engineering", for the dashboard
    tier_reason   TEXT,
    status        TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','skipped','queued','applied','failed','needs_review')),
    run_id        INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    scraped_at    TEXT NOT NULL,
    enriched_at   TEXT,
    scored_at     TEXT,
    classified_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_scraped ON jobs(scraped_at);
CREATE INDEX IF NOT EXISTS idx_jobs_run ON jobs(run_id);

-- ------------------------------------------------------------- the fact base --

CREATE TABLE IF NOT EXISTS resume_base (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    content_sha TEXT NOT NULL UNIQUE,
    parsed_text TEXT NOT NULL,
    uploaded_at TEXT NOT NULL
);

-- One addressable claim. `source` records who put it here; both are equally
-- usable when tailoring, which is what lets the user add a keyword without
-- editing their resume.
CREATE TABLE IF NOT EXISTS resume_facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    base_id    INTEGER REFERENCES resume_base(id) ON DELETE SET NULL,
    kind       TEXT NOT NULL
        CHECK (kind IN ('role','project','skill','education','credential','summary')),
    text       TEXT NOT NULL,
    detail     TEXT,                         -- JSON: employer, dates, metrics, tech
    tags       TEXT,                         -- JSON array of lowercase keywords
    source     TEXT NOT NULL DEFAULT 'parsed'
        CHECK (source IN ('parsed','user_added')),
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_facts_active ON resume_facts(active, kind);

-- Technologies the user has not used. Excluded from every generated variant.
CREATE TABLE IF NOT EXISTS never_claim (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    term       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    note       TEXT,
    created_at TEXT NOT NULL
);

-- One attempt to tailor the resume to one job. `content` is the model's plan
-- (which facts, in what order, phrased how); the PDF is rendered from it and
-- the facts, never from free text. Rejected attempts are kept with their issues.
CREATE TABLE IF NOT EXISTS resume_variants (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id           TEXT REFERENCES jobs(id) ON DELETE CASCADE,
    base_id          INTEGER REFERENCES resume_base(id) ON DELETE SET NULL,
    facts_used       TEXT NOT NULL,          -- JSON array of resume_facts.id
    keyword_coverage REAL,                   -- share of posting keywords the variant covers
    base_coverage    REAL,                   -- the same share for the uploaded resume
    keywords         TEXT,                   -- JSON array: posting keywords considered
    keywords_missing TEXT,                   -- JSON array: keywords no fact supports
    content          TEXT,                   -- JSON TailoredResume
    cover_letter     TEXT,                   -- JSON CoverLetter, or NULL
    status           TEXT NOT NULL DEFAULT 'ready' CHECK (status IN ('ready','rejected')),
    issues           TEXT,                   -- JSON array of validator issues
    pdf_path         TEXT,
    tokens_in        INTEGER NOT NULL DEFAULT 0,
    tokens_out       INTEGER NOT NULL DEFAULT 0,
    attempts         INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_variants_job ON resume_variants(job_id);

-- ------------------------------------------------------------- applications --

CREATE TABLE IF NOT EXISTS applications (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id            TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    variant_id        INTEGER REFERENCES resume_variants(id) ON DELETE SET NULL,
    cover_letter_path TEXT,
    mode              TEXT NOT NULL CHECK (mode IN ('dry_run','review','auto')),
    ats               TEXT,
    screenshot_path   TEXT,
    submitted_at      TEXT,
    UNIQUE (job_id)
);

-- Append-only. Never UPDATE a row here.
CREATE TABLE IF NOT EXISTS application_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL CHECK (kind IN ('status_change','note','email')),
    from_status    TEXT,
    to_status      TEXT
        CHECK (to_status IS NULL OR to_status IN
            ('applied','screening','interviewing','offer','rejected','withdrawn')),
    note           TEXT,
    source         TEXT CHECK (source IS NULL OR source IN ('manual','email','campaign')),
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_app ON application_events(application_id, created_at);

-- Current status per application, derived rather than stored.
CREATE VIEW IF NOT EXISTS application_status AS
SELECT a.id AS application_id,
       a.job_id,
       COALESCE((
           SELECT e.to_status FROM application_events e
           WHERE e.application_id = a.id AND e.to_status IS NOT NULL
           ORDER BY e.created_at DESC, e.id DESC LIMIT 1
       ), 'applied') AS status,
       (SELECT MIN(e.created_at) FROM application_events e
        WHERE e.application_id = a.id AND e.kind = 'email') AS first_reply_at
FROM applications a;

-- ------------------------------------------------------------------ answers --

CREATE TABLE IF NOT EXISTS answers (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    key              TEXT NOT NULL UNIQUE,
    question_pattern TEXT,
    value            TEXT NOT NULL,
    confidence       REAL NOT NULL DEFAULT 1.0,
    updated_at       TEXT NOT NULL
);

