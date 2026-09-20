"""Fetch a posting's full description when a source only returned a snippet.

The board scrapers hand back a few hundred characters for Indeed and LinkedIn,
and a Greenhouse or Ashby board can list a role with no body at all. The
fallback chain is job-agent's: a JSON-LD `JobPosting` block first, because it
is the description exactly as the employer published it; then the platform's
own description container; then, only when a completer is given, the model
reading the page text. Every step has to clear `min_chars`, so a cookie
banner or a header that happens to match a selector cannot pass as a posting.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import httpx
from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, Field

from jobagent.discovery.http import get_text, make_client
from jobagent.discovery.text import html_to_text
from jobagent.llm.backend import Completer, LLMError

log = logging.getLogger(__name__)

# Ordered: the platform containers first, then what a plain site may use.
# "article" and "main" come last because they often wrap the whole page.
DESCRIPTION_SELECTORS: tuple[str, ...] = (
    "#content",  # Greenhouse
    ".posting-page .section-wrapper, .posting",  # Lever
    "[data-qa='job-description']",
    ".ashby-job-posting-content, [class*='JobPostingContent']",  # Ashby
    "#jobDescriptionText",  # Indeed
    ".jobs-description__content, .description__text",  # LinkedIn
    "[itemprop='description']",
    "article",
    "main",
)

MAX_MODEL_CHARS = 30_000

_SYSTEM = """You extract a job description from the text of a web page.

Return the posting's description text as it appears on the page: the role, \
responsibilities, requirements, benefits and compensation sections, in the \
page's own words and order. Do not summarise, do not rewrite, do not add \
anything. Leave out navigation, cookie notices, application forms, footers \
and any other postings listed on the page.

Set found=false and leave description empty if the page is not a job posting \
or the description is not present in the text."""


class ExtractedPosting(BaseModel):
    description: str = Field(description="The description verbatim, or empty when not found.")
    found: bool = Field(description="False when the page is not a posting or has no description.")


def enrich_description(
    url: str,
    *,
    client: httpx.Client | None = None,
    completer: Completer | None = None,
    min_chars: int = 400,
) -> str | None:
    """The posting's description as plain text, or None when the page yields none."""
    own_client = client is None
    client = client or make_client()
    try:
        html = get_text(client, url)
    except httpx.HTTPError as exc:
        log.warning("enrich: could not fetch %s: %s", url, exc)
        return None
    finally:
        if own_client:
            client.close()

    soup = BeautifulSoup(html, "html.parser")

    text = _jsonld_description(soup)
    if text is not None and len(text) >= min_chars:
        return text
    log.debug("enrich: %s has no JSON-LD description of %d+ chars", url, min_chars)

    text = _css_description(soup, min_chars)
    if text is not None:
        return text
    log.debug("enrich: %s has no selector match of %d+ chars", url, min_chars)

    if completer is None:
        return None
    return _model_description(url, _page_text(soup, html), completer, min_chars)


# ----------------------------------------------------------------- JSON-LD --


def extract_jsonld_description(html: str) -> str | None:
    """The description of the first JSON-LD JobPosting on the page, as plain text."""
    return _jsonld_description(BeautifulSoup(html, "html.parser"))


def _jsonld_description(soup: BeautifulSoup) -> str | None:
    for script in soup.find_all("script", type=_is_ld_json):
        try:
            data = json.loads(script.get_text(), strict=False)
        except ValueError:
            log.debug("enrich: skipping a JSON-LD block that is not JSON")
            continue
        for posting in _job_postings(data):
            description = posting.get("description")
            if isinstance(description, str):
                text = html_to_text(description)
                if text:
                    return text
    return None


def _is_ld_json(value: str | None) -> bool:
    return bool(value) and value.strip().lower().startswith("application/ld+json")


def _job_postings(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, list):
        for item in node:
            yield from _job_postings(item)
    elif isinstance(node, dict):
        if _is_job_posting(node):
            yield node
        # A page may publish one @graph of everything on it, or a WebPage whose
        # mainEntity is the posting.
        for key in ("@graph", "mainEntity"):
            if key in node:
                yield from _job_postings(node[key])


def _is_job_posting(node: dict[str, Any]) -> bool:
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return any(isinstance(k, str) and k.strip().rsplit("/", 1)[-1] == "JobPosting" for k in kinds)


# --------------------------------------------------------------------- CSS --


def extract_css_description(html: str, min_chars: int) -> str | None:
    """Text of the first known description container holding at least `min_chars`."""
    return _css_description(BeautifulSoup(html, "html.parser"), min_chars)


def _css_description(soup: BeautifulSoup, min_chars: int) -> str | None:
    for selector in DESCRIPTION_SELECTORS:
        for element in soup.select(selector):
            text = html_to_text(str(element))
            if len(text) >= min_chars:
                return text
    return None


# ------------------------------------------------------------------- model --


def _page_text(soup: BeautifulSoup, html: str) -> str:
    body = soup.body
    text = html_to_text(str(body)) if isinstance(body, Tag) else html_to_text(html)
    return text[:MAX_MODEL_CHARS]


def _model_description(
    url: str, page_text: str, completer: Completer, min_chars: int
) -> str | None:
    # Half the gate: the model strips boilerplate the selectors keep, so a
    # genuine description can come back shorter than the container did.
    floor = min_chars // 2
    if len(page_text) < floor:
        return None
    try:
        completion = completer.complete(
            system=_SYSTEM,
            prompt=f"URL: {url}\n\n<page>\n{page_text}\n</page>",
            output=ExtractedPosting,
            effort="low",
        )
    except LLMError as exc:
        log.warning("enrich: model could not read %s: %s", url, exc)
        return None

    posting = completion.result
    if not isinstance(posting, ExtractedPosting) or not posting.found:
        log.debug("enrich: model found no description at %s", url)
        return None
    description = posting.description.strip()
    if len(description) < floor:
        log.debug("enrich: model description for %s is %d chars, too short", url, len(description))
        return None
    return description
