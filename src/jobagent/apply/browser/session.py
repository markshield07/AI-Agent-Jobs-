"""Launching the browser. Playwright is imported here and nowhere else in the
package, so everything above this module can be tested without it.

The session hands out pages; a handler works on one page and closes it. The
proxy comes from the environment (HTTPS_PROXY), the Chromium binary from
settings when Playwright's own download is absent, and the viewport and user
agent are those of an ordinary desktop browser.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Protocol

from jobagent.config import Settings

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)
VIEWPORT = {"width": 1280, "height": 1600}
DEFAULT_TIMEOUT_MS = 30_000


class BrowserSession(Protocol):
    """What the apply run needs from a browser: fresh pages, one at a time."""

    def new_page(self) -> AbstractContextManager[Any]: ...


class BrowserUnavailable(RuntimeError):
    """Playwright or its browser is not installed here."""


class PlaywrightSession:
    def __init__(self, context: Any) -> None:
        self._context = context

    @contextmanager
    def new_page(self) -> Iterator[Any]:
        page = self._context.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)
        try:
            yield page
        finally:
            page.close()


def launch_options(settings: Settings) -> dict[str, Any]:
    options: dict[str, Any] = {"headless": settings.headless}
    if settings.browser_executable:
        options["executable_path"] = settings.browser_executable
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        options["proxy"] = {"server": proxy}
    return options


@contextmanager
def open_browser(settings: Settings) -> Iterator[BrowserSession]:
    """A Chromium session for one apply run. Raises BrowserUnavailable when it cannot start."""
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserUnavailable(
            "Playwright is not installed. Run: pip install playwright, "
            "then: playwright install chromium"
        ) from exc

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(**launch_options(settings))
        except PlaywrightError as exc:
            raise BrowserUnavailable(
                "Chromium could not start. Run `playwright install chromium`, or set "
                f"JOBAGENT_BROWSER_EXECUTABLE to a Chromium binary. ({exc})"
            ) from exc
        try:
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport=VIEWPORT,
                locale="en-US",
                accept_downloads=False,
            )
            try:
                yield PlaywrightSession(context)
            finally:
                context.close()
        finally:
            browser.close()
