import os

import pytest

from app.db import apply_schema, connect

TABLES = "matches, runs, resumes, postings"


@pytest.fixture(scope="session")
def _conn():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    os.environ["DATABASE_URL"] = url
    conn = connect()
    apply_schema(conn)
    conn.autocommit = True
    yield conn
    conn.close()


@pytest.fixture
def db(_conn):
    _conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
    return _conn
