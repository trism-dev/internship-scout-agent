import psycopg
import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import apply_schema
from app.main import app


def test_health_ok(db):
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_503_when_db_down(monkeypatch):
    def boom():
        raise psycopg.OperationalError("secret host unreachable")

    monkeypatch.setattr(main, "connect", boom)
    r = TestClient(app).get("/health")
    assert r.status_code == 503
    assert r.json() == {"status": "db unavailable"}


def test_schema_is_idempotent(db):
    apply_schema(db)
    apply_schema(db)


def test_score_over_100_rejected(db):
    pid = db.execute(
        "INSERT INTO postings (source, external_id, company, title, url)"
        " VALUES ('s', '1', 'c', 't', 'https://x') RETURNING id"
    ).fetchone()["id"]
    rid = db.execute("INSERT INTO resumes (text) VALUES ('r') RETURNING id").fetchone()["id"]
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO matches (posting_id, resume_id, score, summary, model)"
            " VALUES (%s, %s, 101, 's', 'm')",
            (pid, rid),
        )
