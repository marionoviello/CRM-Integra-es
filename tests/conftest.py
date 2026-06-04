"""Shared test fixtures."""

from collections.abc import Iterator

import pytest

from noviello_funil.db import connect, run_migrations


@pytest.fixture
def db_conn() -> Iterator:
    """In-memory SQLite connection with schema applied."""
    conn = connect(":memory:")
    run_migrations(conn)
    yield conn
    conn.close()
