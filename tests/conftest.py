from __future__ import annotations

import pytest

from jobagent.config import Settings
from jobagent.db.database import open_database


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path, anthropic_api_key="test-key")


@pytest.fixture
def db(settings):
    database = open_database(settings.db_path)
    yield database
    database.close()


@pytest.fixture
def conn(db):
    return db.connection()


# ------------------------------------------------------------ browser --
#
# Handler tests drive real Chromium against fixture pages under tests/fixtures.
# Without a browser they skip, unless JOBAGENT_REQUIRE_BROWSER is set (CI sets
# it, so a missing browser there is a failure, not a silent skip).


@pytest.fixture(scope="session")
def browser_session(tmp_path_factory):
    import os

    from jobagent.apply.browser.session import BrowserUnavailable, open_browser

    settings = Settings(
        data_dir=tmp_path_factory.mktemp("browser"),
        browser_executable=os.environ.get("JOBAGENT_BROWSER_EXECUTABLE") or None,
    )
    try:
        with open_browser(settings) as session:
            yield session
    except BrowserUnavailable as exc:
        if os.environ.get("JOBAGENT_REQUIRE_BROWSER"):
            raise
        pytest.skip(f"no browser here: {exc}")


@pytest.fixture
def page(browser_session):
    with browser_session.new_page() as p:
        yield p
