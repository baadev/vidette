"""Shared fixtures for the server test suite."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from vidette.core.config import VidetteConfig
from vidette.db import Database
from vidette.telegram import i18n

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def bot_language() -> Iterator[None]:
    """The suite was written against the Russian catalog: tests read Russian unless they
    choose otherwise (English has its own tests, test_i18n.py). A bot built from a config
    sets its own language."""
    i18n.use("ru")
    yield
    i18n.use("en")


@pytest.fixture
def media_dir(tmp_path: Path) -> Path:
    path = tmp_path / "media"
    path.mkdir()
    return path


@pytest.fixture
def test_config(tmp_path: Path, media_dir: Path) -> VidetteConfig:
    """Frigate source + tmp storage; keep in sync with schema defaults, not the example."""
    return VidetteConfig.model_validate(
        {
            "storage": {
                "media_dir": str(media_dir),
                "database": str(tmp_path / "vidette.db"),
            },
            "frigate": {
                "url": "http://frigate.test:5000",
                "mqtt": {"host": "broker.test"},
            },
        }
    )


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()
