"""What a posting asks for, and how much of it a text covers.

Adapted from job-pilot's ATS keyword target. A posting's keywords come from
three sources: a lexicon of terms postings commonly ask for, whatever in the
text reads as a name or a technology (`keyword_like`), and the user's own
search keywords when the posting mentions them. Ranked by how often the posting
repeats them, they are the target a tailored resume is measured against:
`coverage` is the share found in a text and `split_by_support` says which of
them the fact base can honestly supply, so the model is told what to lead with
and never asked to invent the rest.

Every match goes through `terms.contains_term`, so a keyword counted here is one
the validator and the coverage number will find the same way.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from jobagent.tailor.terms import STOPWORDS, _pattern, contains_term, keyword_like, numbers_in

# Terms a posting commonly asks for. Bare "go", "rest" and "excel" are left out
# on purpose: as lowercase words they are prose ("ready to go", "the rest of the
# team", "excel at") and `keyword_like` catches the capitalised forms anyway.
# "c" and "r" are one letter, which no source may keep.
TECH_LEXICON: frozenset[str] = frozenset(
    (
        # languages
        "python", "java", "typescript", "javascript", "golang", "rust", "c++", "c#", "kotlin",
        "swift", "objective-c", "ruby", "php", "scala", "dart", "matlab", "sql", "nosql",
        "pl/sql", "t-sql", "bash", "powershell", "shell scripting", "html", "css",
        # web and application frameworks
        "react", "react native", "angular", "vue", "vue.js", "svelte", "next.js", "nextjs",
        "node.js", "nodejs", "django", "flask", "fastapi", "celery", "sqlalchemy", "spring",
        "spring boot", ".net", ".net core", "asp.net", "rails", "ruby on rails", "laravel",
        "flutter", "redux", "tailwind", "bootstrap", "graphql", "grpc", "websockets", "restful",
        "android", "ios",
        # data, ml and analytics
        "pytorch", "tensorflow", "keras", "scikit-learn", "pandas", "numpy", "jupyter", "spark",
        "pyspark", "hadoop", "kafka", "airflow", "dbt", "databricks", "langchain", "hugging face",
        "machine learning", "deep learning", "mlops", "nlp", "natural language processing",
        "computer vision", "llm", "llms", "generative ai", "rag", "etl", "elt", "data modeling",
        "data modelling", "data pipeline", "data pipelines", "data engineering", "data analysis",
        "data visualization", "data visualisation", "data warehouse", "data lake", "statistics",
        "a/b testing", "tableau", "power bi", "looker", "google analytics", "google ads", "seo",
        "sem",
        # data stores and messaging
        "postgres", "postgresql", "mysql", "mariadb", "sqlite", "sql server", "mssql", "oracle",
        "mongodb", "mongo", "redis", "memcached", "elasticsearch", "opensearch", "snowflake",
        "bigquery", "redshift", "dynamodb", "cassandra", "neo4j", "firebase", "firestore",
        "rabbitmq", "sqs", "s3",
        # cloud and infrastructure
        "aws", "amazon web services", "gcp", "google cloud", "azure", "kubernetes", "k8s",
        "docker", "terraform", "pulumi", "cloudformation", "ansible", "helm", "linux", "unix",
        "nginx", "serverless", "lambda", "ec2", "ecs", "eks", "gke", "infrastructure as code",
        "iac", "gitops", "argocd", "argo cd", "devops", "devsecops", "sre", "site reliability",
        "tcp/ip", "dns", "vpn", "load balancing", "cdn", "iam",
        # practices and tooling
        "ci/cd", "continuous integration", "continuous delivery", "continuous deployment", "git",
        "github", "gitlab", "bitbucket", "jenkins", "github actions", "agile", "scrum", "kanban",
        "tdd", "bdd", "unit testing", "integration testing", "test automation", "selenium",
        "cypress", "playwright", "jest", "pytest", "microservices", "event-driven", "oauth",
        "oauth2", "sso", "jwt", "observability", "prometheus", "grafana", "datadog", "splunk",
        "new relic", "sentry", "pagerduty", "opentelemetry", "kibana", "jira", "confluence",
        "design patterns", "system design", "distributed systems", "data structures",
        "algorithms", "object-oriented", "oop", "functional programming", "concurrency",
        "multithreading", "code review", "code reviews", "pair programming", "cybersecurity",
        "application security", "cloud security", "network security", "information security",
        "penetration testing", "owasp", "encryption", "soc 2", "gdpr", "hipaa", "accessibility",
        "responsive design", "ui/ux", "ux", "figma", "adobe", "user research",
        # non-engineering skills and tools
        "microsoft excel", "ms excel", "powerpoint", "microsoft office", "google sheets",
        "google workspace", "salesforce", "sap", "hubspot", "zendesk", "servicenow", "workday",
        "crm", "erp", "project management", "program management", "product management",
        "stakeholder management", "vendor management", "change management", "risk management",
        "account management", "budgeting", "forecasting", "financial modeling",
        "financial modelling", "financial analysis", "p&l", "recruiting", "sourcing",
        "onboarding", "negotiation", "contract negotiation", "procurement", "supply chain",
        "logistics", "lean", "six sigma", "pmp", "okrs", "kpis", "customer success",
        "customer service", "business development", "sales", "marketing", "content marketing",
        "social media", "coaching", "mentoring", "accounting",
    )
)  # fmt: skip

# Symbol-carrying tokens `keyword_like` keeps that name nothing.
_NOISE: frozenset[str] = frozenset({"e.g", "i.e"})

# A sentence ends at ".", "!" or "?" followed by space or end of text, except
# after "e.g." and "i.e.", and at every line break: bullet lines rarely end
# with a period.
_SENTENCE_BREAK = re.compile(r"(?<!e\.g)(?<!i\.e)[.!?](?=\s|$)|\n")
_COMPOUND_JOIN = re.compile(r"[/-]")


def posting_keywords(
    description: str | None,
    title: str | None = None,
    *,
    extra: Iterable[str] = (),
    exclude: Iterable[str] = (),
    limit: int = 60,
) -> list[str]:
    """The terms a posting asks for, most repeated first, then by first appearance.

    Three sources are unioned: every `TECH_LEXICON` term the text contains, every
    `keyword_like` token of the text, and every `extra` term (the user's search
    keywords) the text contains. Dropped: pure numbers, single characters,
    stopwords, anything in `exclude` (pass the company name; its words are
    excluded too, so a posting's own brand is never a keyword), and a
    `keyword_like` token that only repeats a longer term already present, such as
    "power" next to "power bi" or "python/java" next to "python" and "java".
    """
    if limit < 0:
        raise ValueError("limit must not be negative")
    text = "\n".join(part for part in (title or "", description or "") if part.strip())
    if not text:
        return []

    excluded = _exclusions(exclude)

    def usable(term: str) -> bool:
        return (
            len(term) >= 2
            and term not in STOPWORDS
            and term not in _NOISE
            and term not in excluded
            and not _is_number(term)
        )

    lexicon_hits = [t for t in sorted(TECH_LEXICON) if usable(t) and contains_term(text, t)]
    extra_hits = [t for t in _clean(extra) if usable(t) and contains_term(text, t)]
    names = [t for t in _names_in(text) if usable(t)]

    # A name that is only a piece of a longer term the posting asks for says
    # nothing on its own; a name that only glues present terms together says
    # nothing new. Both rules apply to `keyword_like` tokens alone: the lexicon
    # and the user's own terms are kept as given.
    longer = [*lexicon_hits, *extra_hits]
    kept = {*lexicon_hits, *extra_hits, *names}
    names = [
        token for token in names if not _fragment_of(token, longer) and not _glued(token, kept)
    ]

    candidates = dict.fromkeys([*lexicon_hits, *names, *extra_hits])
    ranked = sorted(candidates, key=lambda term: _rank(text, term))
    return ranked[:limit]


def coverage(text: str, keywords: Sequence[str]) -> float:
    """The share of `keywords` present in `text`, 0.0 when there are none."""
    wanted = [keyword for keyword in keywords if keyword and keyword.strip()]
    if not wanted:
        return 0.0
    hits = sum(1 for keyword in wanted if contains_term(text, keyword))
    return round(hits / len(wanted), 3)


def split_by_support(keywords: Sequence[str], fact_pool: str) -> tuple[list[str], list[str]]:
    """(supported, missing), each in the order given: supported means the facts say it."""
    supported: list[str] = []
    missing: list[str] = []
    for keyword in keywords:
        (supported if contains_term(fact_pool, keyword) else missing).append(keyword)
    return supported, missing


# ----------------------------------------------------------------- helpers --


def _clean(values: Iterable[str]) -> list[str]:
    """Lowercased, single-spaced, non-blank, distinct, in the order given."""
    seen: dict[str, None] = {}
    for value in values:
        cleaned = " ".join((value or "").lower().split())
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen)


def _exclusions(exclude: Iterable[str]) -> set[str]:
    """Each excluded term and each of its words, so "Acme Robotics" drops "acme" too."""
    excluded: set[str] = set()
    for term in _clean(exclude):
        excluded.add(term)
        excluded.update(term.split())
    return excluded


def _names_in(text: str) -> list[str]:
    """`keyword_like` over each sentence and line separately, distinct, in order.

    `keyword_like` treats a capitalised word as a name unless it starts a
    sentence, but its tokeniser swallows a sentence-ending period into the word
    before it, so after the first sentence every capitalised word passes.
    Splitting first restores the documented behaviour.
    """
    seen: dict[str, None] = {}
    for chunk in _SENTENCE_BREAK.split(text):
        for token in keyword_like(chunk):
            seen.setdefault(token, None)
    return list(seen)


def _is_number(term: str) -> bool:
    return term in numbers_in(term)


def _fragment_of(token: str, terms: Iterable[str]) -> bool:
    """True when `token` is a whole-word part of a longer term in `terms`."""
    return any(len(term) > len(token) and contains_term(term, token) for term in terms)


def _glued(token: str, kept: set[str]) -> bool:
    """True for "python/java" or "day-to-day": every part is kept already or a stopword."""
    parts = _COMPOUND_JOIN.split(token)
    if len(parts) < 2:
        return False
    return all(part in kept or part in STOPWORDS or len(part) < 2 for part in parts)


def _rank(text: str, term: str) -> tuple[int, int, int]:
    """Most occurrences first, then earliest, then longest (the more specific term).

    Counting needs the same boundaries `contains_term` uses, so the pattern is
    shared rather than rewritten; `terms` exposes no counter.
    """
    matches = list(_pattern(term).finditer(text))
    if not matches:
        return 0, len(text), -len(term)
    return -len(matches), matches[0].start(), -len(term)
