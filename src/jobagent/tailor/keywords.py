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
        # networking, data centers and IT operations
        "network engineering", "network operations", "network administration",
        "network infrastructure", "network design", "network monitoring", "network automation",
        "network architecture", "routing", "switching", "routing and switching", "routers",
        "switches", "firewall", "firewalls", "load balancer", "load balancers", "lan", "wan",
        "lan/wan", "sd-wan", "wlan", "wireless", "wi-fi", "vlan", "vlans", "vxlan", "bgp",
        "ospf", "eigrp", "mpls", "stp", "hsrp", "vrrp", "qos", "dhcp", "ipv4", "ipv6", "ipsec",
        "snmp", "netflow", "sase", "zero trust", "cisco", "cisco ios", "nx-os", "nexus",
        "catalyst", "meraki", "juniper", "junos", "arista", "palo alto", "fortinet", "fortigate",
        "check point", "f5", "aruba", "infoblox", "zscaler", "solarwinds", "wireshark", "netbox",
        "prtg", "nagios", "zabbix", "logicmonitor", "ccna", "ccnp", "ccie", "jncia", "jncip",
        "network+", "comptia", "itil", "noc", "incident management", "problem management",
        "root cause analysis", "troubleshooting", "disaster recovery", "business continuity",
        "capacity planning", "data center", "data centers", "datacenter", "colocation",
        "structured cabling", "fiber", "vmware", "vsphere", "hyper-v", "active directory",
        "windows server", "microsoft 365", "office 365", "slas", "sla", "change control",
        "it operations", "operations management", "people management", "field engineering",
        "field operations", "network deployment", "data center operations", "turn-up",
        "bring-up", "burn-in", "power-on", "break-fix", "rma", "rack and stack",
        "cross-connect", "cross-connects", "optics", "colocation", "hpc", "gpu", "on-call",
        "incident response", "out-of-band", "802.1x", "mdf", "idf", "msp", "msps",
        "low-voltage", "segmentation", "telemetry", "ticketing", "ai", "automation", "workflows",
        "integrations",
    )
)  # fmt: skip

# Words a posting capitalises that name nothing a resume could show: equal-
# opportunity and benefits boilerplate, how to apply, the parts of a job ad,
# places, days and months. They only drop `keyword_like` tokens; a lexicon
# term or the user's own keyword is never dropped here.
BOILERPLATE: frozenset[str] = frozenset(
    """
    equal opportunity opportunities employer employers eeo eoe eeoc affirmative action race
    color colour religion creed sex sexual gender orientation identity expression national
    origin ancestry age disability disabilities veteran veterans protected genetic marital
    pregnancy citizenship status benefits benefit medical dental vision insurance pto 401k
    holidays holiday vacation wellness bonus compensation salary pay paid perks hsa fsa eap
    apply applicant applicants candidate candidates legally authorized authorization
    sponsorship visa terms privacy notice policy consent accommodation accommodations
    reasonable e-verify background drug click submit resume cv now please about title
    qualifications qualification preferred required minimum summary description duties
    overview position positions job jobs opening schedule shift hours time full-time
    part-time contract contract-to-hire temporary permanent w2 w-2 c2c 1099 hybrid onsite
    on-site remote local area location locations travel need needs basis world global
    international environment united states usa u.s u.s.a america american north south east
    west city county metro valley bay monday tuesday wednesday thursday friday saturday
    sunday january february march april june july august september october november
    december inc llc ltd corp corporation al ak az ar ca co ct de fl ga hi id il ia ks ky la
    md ma mi mn ms mo mt ne nv nh nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv wi
    wy dc include includes including earnings ote commission commissions usd equity stock
    retirement savings spending accounts san francisco york seattle london
    """.split()
)

# Hyphenated words that describe a person or a workplace, not a skill: "hands-on"
# is in nearly every posting and on no resume as a claim the facts could back.
SOFT_COMPOUNDS: frozenset[str] = frozenset(
    """
    hands-on fast-paced detail-oriented results-driven results-oriented solution-oriented
    self-starter self-motivated self-reported cross-functional world-class best-in-class
    high-quality high-impact high-performing high-speed high-density long-term short-term
    day-to-day well-being end-to-end next-generation cutting-edge state-of-the-art
    forward-thinking late-stage program-wide fleet-wide company-wide boots-on-the-ground
    on-the-floor on-site in-office in-person full-time part-time year-round multi-year
    hands-on-keyboard
    """.split()
)

# A section that says who the company is or what it pays, not what the job needs.
# It runs from one of these lines to the next on-topic heading.
_OFF_TOPIC = re.compile(
    r"^(?:about (?!the role|the job|the position|the team|this role|the opportunity|you\b)"
    r"|benefits|perks|compensation|annual salary|salary|pay range|pay transparency|logistics"
    r"|equal (?:employment )?opportunity|how we.?re different|come work with us"
    r"|our (?:values|benefits|culture)|who we are|why (?:join|work)|life at"
    r"|the (?:annual )?(?:compensation|salary|pay) range|for sales roles)",
    re.IGNORECASE,
)
_ON_TOPIC = re.compile(
    r"^(?:about (?:the role|the job|the position|the team|this role|the opportunity|you)"
    r"|the role|responsibilit|what you.?ll|what you will|you may be|you will|you.?ll"
    r"|requirements|qualifications|strong candidates|it.?s a bonus|bonus points"
    r"|nice to have|technical skills|representative work|duties|what we.?re looking for"
    r"|who you are)",
    re.IGNORECASE,
)
_HEADING_CHARS = 60

# Paragraphs a careless HTML flattening glued together: "what's next.Open Connect",
# "Qualifications:8+ years". A capital or digit straight after the stop starts a
# new sentence; "node.js" and "ASP.NET" do not match.
_GLUED_SENTENCE = re.compile(r"(?<=[a-z0-9)])[.:;](?=[A-Z])|(?<=[a-z)])[.:;](?=\d)")

_CLOCK = re.compile(r"^\d{1,2}(?::\d\d)?(?:am|pm)$")
_WEB = re.compile(r"(?:^www\.|\.(?:com|net|org|io|gov|edu|us)$|@)")

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
    text = "\n".join(part for part in (title or "", _on_topic(description or "")) if part.strip())
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
    names = [
        t
        for t in _names_in(text)
        if usable(t)
        and not _boilerplate(t, text)
        and not _soft_compound(t, text)
        and not _names_the_company(t, excluded)
    ]

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


def coverage(text: str, keywords: Sequence[str], *, of: Sequence[str] | None = None) -> float:
    """The share of `keywords` present in `text`, 0.0 when there are none.

    `of` is the list the share is taken of, when it differs from the list
    counted: the hits among `keywords`, divided by the size of `of`.
    """
    wanted = [keyword for keyword in keywords if keyword and keyword.strip()]
    total = len([k for k in of if k and k.strip()]) if of is not None else len(wanted)
    if not total:
        return 0.0
    hits = sum(1 for keyword in wanted if contains_term(text, keyword))
    return round(hits / total, 3)


def split_by_support(keywords: Sequence[str], fact_pool: str) -> tuple[list[str], list[str]]:
    """(supported, missing), each in the order given: supported means the facts say it."""
    supported: list[str] = []
    missing: list[str] = []
    for keyword in keywords:
        (supported if contains_term(fact_pool, keyword) else missing).append(keyword)
    return supported, missing


# ----------------------------------------------------------------- helpers --


def _on_topic(description: str) -> str:
    """The posting without its about-the-company and pay sections, sentences unglued.

    A stored description can have lost its paragraph breaks ("next.Open
    Connect") or its curly apostrophes (U+FFFD), so both are repaired first.
    If dropping sections would leave nothing, the whole text is kept.
    """
    text = _GLUED_SENTENCE.sub(lambda m: m.group(0) + "\n", description.replace("\ufffd", "'"))
    kept: list[str] = []
    skipping = False
    for line in text.splitlines():
        stripped = line.strip()
        if _OFF_TOPIC.match(stripped):
            skipping = True
        elif skipping and len(stripped) <= _HEADING_CHARS and _ON_TOPIC.match(stripped):
            skipping = False
        if not skipping:
            kept.append(line)
    return "\n".join(kept) if any(line.strip() for line in kept) else text


def _soft_compound(token: str, text: str) -> bool:
    """True for a hyphenated word that is a trait, or one the posting says only once.

    A lexicon term never reaches this check, so "turn-up" and "break-fix" are kept
    however often they appear; an unknown compound has to be repeated to count.
    """
    if "-" not in token or not token.replace("-", "").isalpha():
        return False
    return token in SOFT_COMPOUNDS or len(_pattern(token).findall(text)) < 2


def _names_the_company(token: str, excluded: set[str]) -> bool:
    """True for "anthropic-owned", "anthropic's" or "anthropics" when "anthropic" is excluded."""
    parts = [p for p in re.split(r"[/'-]", token) if p]
    return any(p in excluded or (p.endswith("s") and p[:-1] in excluded) for p in parts)


def _clean(values: Iterable[str]) -> list[str]:
    """Lowercased, single-spaced, non-blank, distinct, in the order given."""
    seen: dict[str, None] = {}
    for value in values:
        cleaned = " ".join((value or "").lower().split())
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen)


def _exclusions(exclude: Iterable[str]) -> set[str]:
    """Each excluded term and each of its words, so "Acme Robotics" drops "acme" too.

    Commas and brackets split words as spaces do, so a location such as
    "Irvine, CA (Hybrid)" drops "irvine", "ca" and "hybrid".
    """
    excluded: set[str] = set()
    for term in _clean(exclude):
        term = term.strip(" ,;()")
        if not term:
            continue
        excluded.add(term)
        excluded.update(word for word in re.split(r"[\s,;()]+", term) if word)
    return excluded


def _boilerplate(token: str, text: str) -> bool:
    """True for a `keyword_like` token that names nothing a resume could show.

    That is a `BOILERPLATE` word, or a compound made only of them
    ("m/f/disability/veterans"), a clock time, a web address, or a plain
    capitalised word the posting also uses in lower case, which makes it an
    ordinary word in a heading ("Management" beside "change management").
    """
    if token in BOILERPLATE or _CLOCK.match(token) or _WEB.search(token):
        return True
    parts = [p for p in _COMPOUND_JOIN.split(token) if p]
    if len(parts) > 1 and all(p in BOILERPLATE or p in STOPWORDS or len(p) < 2 for p in parts):
        return True
    if not token.isalpha():
        return False
    return re.search(rf"(?<![\w+#]){re.escape(token)}(?![\w+#])", text) is not None


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
