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
