"""HTML to readable plain text, the same way for every source."""

from __future__ import annotations

import html as html_lib
import re

from bs4 import BeautifulSoup

_BLOCK_TAGS = ("p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section")


def html_to_text(markup: str | None) -> str:
    """Strip tags, keep paragraph breaks, collapse whitespace.

    Greenhouse returns its `content` field HTML-escaped, so entities are
    unescaped first; on already-plain HTML that is a no-op.
    """
    if not markup:
        return ""
    unescaped = html_lib.unescape(markup)
    if "<" not in unescaped:
        return _tidy(unescaped)

    soup = BeautifulSoup(unescaped, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    return _tidy(soup.get_text(" "))


def _tidy(text: str) -> str:
    text = text.replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()
