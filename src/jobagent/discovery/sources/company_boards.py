"""Company job boards worth watching for remote network and IT operations roles.

Networking and security vendors, cloud and hosting providers, data center
operators and managed service companies that hire on Greenhouse or Lever.
Their boards are public JSON (see greenhouse.py and lever.py), so watching
them costs one request a board and finds roles before most people see them on
LinkedIn. Only titles from the search criteria are kept, and only postings
that say they are fully remote get through the rules after that.

`jobagent boards check` says which of these still answer; a board that stops
answering is logged and skipped, never an error. Turned on with
JOBAGENT_COMPANY_BOARDS=true; boards in the search criteria are searched too.
"""

from __future__ import annotations

from jobagent.discovery.criteria import BoardRef, SearchCriteria

# (ats, slug, display name)
CATALOG: tuple[tuple[str, str, str], ...] = (
    # Networking, edge and network security
    ("greenhouse", "cloudflare", "Cloudflare"),
    ("greenhouse", "fastly", "Fastly"),
    ("greenhouse", "netskope", "Netskope"),
    ("greenhouse", "tailscale", "Tailscale"),
    ("greenhouse", "kentik", "Kentik"),
    ("greenhouse", "aviatrix", "Aviatrix"),
    ("greenhouse", "verkada", "Verkada"),
    ("greenhouse", "axonius", "Axonius"),
    ("greenhouse", "arcticwolf", "Arctic Wolf"),
    ("greenhouse", "expel", "Expel"),
    ("greenhouse", "huntress", "Huntress"),
    ("greenhouse", "okta", "Okta"),
    # Cloud, hosting, infrastructure and data center
    ("greenhouse", "coreweave", "CoreWeave"),
    ("greenhouse", "digitalocean", "DigitalOcean"),
    ("greenhouse", "vultr", "Vultr"),
    ("greenhouse", "lambda", "Lambda"),
    ("greenhouse", "crusoe", "Crusoe"),
    ("greenhouse", "purestorage", "Pure Storage"),
    ("greenhouse", "rubrik", "Rubrik"),
    ("greenhouse", "cohesity", "Cohesity"),
    ("greenhouse", "nutanix", "Nutanix"),
    ("greenhouse", "databricks", "Databricks"),
    # Operations, monitoring and IT tooling (NOC / IT ops teams)
    ("greenhouse", "datadog", "Datadog"),
    ("greenhouse", "pagerduty", "PagerDuty"),
    ("greenhouse", "newrelic", "New Relic"),
    ("greenhouse", "grafanalabs", "Grafana Labs"),
    ("greenhouse", "sumologic", "Sumo Logic"),
    ("greenhouse", "twilio", "Twilio"),
    ("greenhouse", "gitlab", "GitLab"),
    ("greenhouse", "elastic", "Elastic"),
    ("greenhouse", "mongodb", "MongoDB"),
    ("greenhouse", "samsara", "Samsara"),
    ("greenhouse", "dropbox", "Dropbox"),
    ("greenhouse", "ninjaone", "NinjaOne"),
    # Managed services and IT services
    ("lever", "rackspace", "Rackspace"),
    ("lever", "kaseya", "Kaseya"),
    ("lever", "connectwise", "ConnectWise"),
    ("lever", "auvik", "Auvik"),
    ("lever", "ui", "Ubiquiti"),
    ("lever", "zerotier", "ZeroTier"),
    ("lever", "netgear", "NETGEAR"),
    ("lever", "veeam", "Veeam"),
)


def catalog_boards() -> list[BoardRef]:
    return [BoardRef(ats=ats, slug=slug, company=name) for ats, slug, name in CATALOG]


def with_catalog(boards: list[BoardRef], ats: set[str]) -> list[BoardRef]:
    """`boards` plus the catalog's boards on `ats`, each board once (the
    criteria's own entry wins, keeping its display name)."""
    have = {(b.ats, b.slug.lower()) for b in boards}
    extra = [b for b in catalog_boards() if b.ats in ats and (b.ats, b.slug.lower()) not in have]
    return [*boards, *extra]


def with_company_boards(criteria: SearchCriteria, apply_sites: tuple[str, ...]) -> SearchCriteria:
    """The criteria with the catalog's boards added, for the ATSs the apply
    sites allow (both, when every site is allowed)."""
    allowed = {"greenhouse", "lever"}
    if apply_sites:
        allowed &= set(apply_sites)
    if not allowed:
        return criteria
    return criteria.model_copy(update={"boards": with_catalog(criteria.boards, allowed)})
