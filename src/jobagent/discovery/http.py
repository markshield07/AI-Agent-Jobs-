"""One HTTP client for every source, so tests can swap the transport.

Sources take an optional `httpx.Client`; tests pass one built on
`httpx.MockTransport` and never touch the network.
"""

from __future__ import annotations

from typing import Any

import httpx

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def make_client(timeout: float = 20.0, **kwargs: Any) -> httpx.Client:
    return httpx.Client(headers=DEFAULT_HEADERS, timeout=timeout, follow_redirects=True, **kwargs)


def get_json(client: httpx.Client, url: str, **params: Any) -> Any:
    response = client.get(url, params=params or None)
    response.raise_for_status()
    return response.json()


def get_text(client: httpx.Client, url: str) -> str:
    response = client.get(url)
    response.raise_for_status()
    return response.text
