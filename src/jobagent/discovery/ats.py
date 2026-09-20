"""Recognise which applicant tracking system a URL belongs to.

Lifted from AutoApply's filter module. The submission stage in phase 4
dispatches on this, so a wrong guess here is a wrong form filler later.
"""

from __future__ import annotations

from urllib.parse import urlparse

# Ordered: more specific hosts first.
ATS_FINGERPRINTS: tuple[tuple[str, str], ...] = (
    ("boards.greenhouse.io", "greenhouse"),
    ("job-boards.greenhouse.io", "greenhouse"),
    ("greenhouse.io", "greenhouse"),
    ("jobs.lever.co", "lever"),
    ("lever.co", "lever"),
    ("jobs.ashbyhq.com", "ashby"),
    ("ashbyhq.com", "ashby"),
    ("myworkdayjobs.com", "workday"),
    ("myworkdaysite.com", "workday"),
    ("icims.com", "icims"),
    ("taleo.net", "taleo"),
    ("smartrecruiters.com", "smartrecruiters"),
    ("workable.com", "workable"),
    ("bamboohr.com", "bamboohr"),
    ("linkedin.com", "linkedin"),
    ("indeed.com", "indeed"),
)


def detect_ats(url: str | None) -> str | None:
    if not url:
        return None
    host = (urlparse(url).netloc or "").lower()
    if not host:
        host = url.lower()
    for fingerprint, ats in ATS_FINGERPRINTS:
        if host == fingerprint or host.endswith("." + fingerprint) or fingerprint in host:
            return ats
    return None
