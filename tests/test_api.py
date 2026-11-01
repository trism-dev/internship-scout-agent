import itertools
import time
from types import SimpleNamespace

import pytest
import test_agent as ta
from fastapi.testclient import TestClient

from app import main
from app.main import app

TOKEN = "tok"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
COUNT = itertools.count(1)
PROTECTED = [
    ("PUT", "/resume", {"json": {"text": "x"}}),
    ("POST", "/runs", {}),
    ("GET", "/matches", {}),
    ("GET", "/matches/1", {}),
]


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("API_TOKEN", TOKEN)


@pytest.fixture
def client(db):
    return TestClient(app)


def add_resume(db, text="Fictional Person, knows Rust."):
    return db.execute("INSERT INTO resumes (text) VALUES (%s) RETURNING id", (text,)).fetchone()["id"]


def add_match(db, resume_id, title="Intern", url="https://example.com/p", score=80):
    pid = db.execute(
        "INSERT INTO postings (source, external_id, company, title, url)"
        " VALUES ('s', %s, 'Acme', %s, %s) RETURNING id",
        (str(next(COUNT)), title, url),
    ).fetchone()["id"]
    return db.execute(
        "INSERT INTO matches (posting_id, resume_id, score, summary, reasons, gaps, model)"
        " VALUES (%s, %s, %s, 'SUMMARY-TEXT', '[{\"requirement\": \"r\", \"evidence\": \"EVIDENCE-TEXT\"}]',"
        " '[\"GAP-TEXT\"]', 'm') RETURNING id",
        (pid, resume_id, score),
    ).fetchone()["id"]


def feed(*ids):
    return [
        {"id": i, "active": True, "company_name": "Acme", "title": f"T{i}", "date_posted": time.time(),
         "url": f"https://example.com/{i}", "locations": [], "terms": [], "category": "SWE",
         "sponsorship": "Other"}
        for i in ids
    ]  # fmt: skip


def use_anthropic(monkeypatch, *responses):
    c, reqs = ta.client(*responses)
    monkeypatch.setattr(main, "anthropic", SimpleNamespace(Anthropic=lambda: c))
    return reqs


@pytest.mark.parametrize(("method", "path", "kw"), PROTECTED)
def test_auth_required(client, monkeypatch, method, path, kw):
    assert client.request(method, path, **kw).status_code == 401
    bad = {"Authorization": "Bearer wrong"}
    assert client.request(method, path, headers=bad, **kw).status_code == 401
    assert client.request(method, path, headers={"Authorization": TOKEN}, **kw).status_code == 401
    for value in ("", None):
        if value is None:
            monkeypatch.delenv("API_TOKEN")
        else:
            monkeypatch.setenv("API_TOKEN", value)
        assert client.request(method, path, headers=AUTH, **kw).status_code == 503
        assert client.request(method, path, headers={"Authorization": "Bearer "}, **kw).status_code == 503


def test_resume_saved_latest_wins_and_not_echoed(client, db):
    r = client.put("/resume", json={"text": "  Fictional Person  "}, headers=AUTH)
    assert r.status_code == 200 and r.json() == {"id": 1}
    client.put("/resume", json={"text": "second resume"}, headers=AUTH)
    assert db.execute("SELECT text FROM resumes ORDER BY id").fetchall() == [
        {"text": "Fictional Person"},
        {"text": "second resume"},
    ]
    assert "Fictional" not in r.text


@pytest.mark.parametrize("text", ["", "   ", "x" * 50_001, "a\x00b"])
def test_resume_validation(client, db, text):
    r = client.put("/resume", json={"text": text}, headers=AUTH)
    assert r.status_code == 422 and (not text.strip() or text[:5] not in r.text)
    assert db.execute("SELECT count(*) AS n FROM resumes").fetchone()["n"] == 0


def test_resume_max_length_accepted(client, db):
    assert client.put("/resume", json={"text": "x" * 50_000}, headers=AUTH).status_code == 200


def test_run_conflicts(client, db):
    assert client.post("/runs", headers=AUTH).status_code == 409
    add_resume(db)
    db.execute("INSERT INTO runs DEFAULT VALUES")
    assert client.post("/runs", headers=AUTH).status_code == 409
    assert db.execute("SELECT count(*) AS n FROM runs").fetchone()["n"] == 1


def test_full_run_then_rerun_scores_nothing_twice(client, db, monkeypatch):
    add_resume(db)
    monkeypatch.setattr(main, "fetch_listings", lambda: feed("a", "b"))
    reqs = use_anthropic(monkeypatch, ta.msg([ta.rec(1, 90), ta.rec(2, 40)], i=10, o=5), ta.end())
    r = client.post("/runs", headers=AUTH)
    assert r.status_code == 202 and r.json() == {"run_id": 1}
    assert "Fictional" in reqs[0]["system"]
    assert client.get("/runs/latest").json() | {"started_at": 0, "finished_at": 0} == {
        "started_at": 0, "finished_at": 0, "status": "succeeded", "postings_fetched": 2,
        "postings_new": 2, "postings_scored": 2, "input_tokens": 20, "output_tokens": 10,
    }  # fmt: skip
    assert db.execute("SELECT finished_at FROM runs").fetchone()["finished_at"] is not None

    use_anthropic(monkeypatch)
    client.post("/runs", headers=AUTH)
    latest = client.get("/runs/latest").json()
    assert (latest["postings_fetched"], latest["postings_new"], latest["postings_scored"]) == (2, 0, 0)
    assert latest["input_tokens"] == 0 and latest["status"] == "succeeded"
    assert db.execute("SELECT count(*) AS n FROM matches").fetchone()["n"] == 2


def test_failed_batches_still_succeed(client, db, monkeypatch):
    add_resume(db)
    monkeypatch.setattr(main, "fetch_listings", lambda: feed("a"))
    use_anthropic(monkeypatch, ta.msg([], "refusal"))
    client.post("/runs", headers=AUTH)
    assert client.get("/runs/latest").json()["status"] == "succeeded"


def test_pipeline_failure_stores_only_class_name(client, db, monkeypatch):
    add_resume(db)

    def boom():
        raise RuntimeError("sk-secret-value")

    monkeypatch.setattr(main, "fetch_listings", boom)
    client.post("/runs", headers=AUTH)
    row = db.execute("SELECT status, error, finished_at FROM runs").fetchone()
    assert row["status"] == "failed" and row["error"] == "RuntimeError" and row["finished_at"]
    r = client.get("/runs/latest")
    assert "error" not in r.json() and "secret" not in r.text and "RuntimeError" not in r.text


def test_startup_marks_leftover_running_failed(db):
    db.execute("INSERT INTO runs DEFAULT VALUES")
    with TestClient(app):
        pass
    row = db.execute("SELECT status, error, finished_at FROM runs").fetchone()
    assert (row["status"], row["error"]) == ("failed", "interrupted") and row["finished_at"]


def test_latest_run_404_and_fields(client, db):
    assert client.get("/runs/latest").status_code == 404
    db.execute("INSERT INTO runs (status, error) VALUES ('failed', 'X')")
    assert set(client.get("/runs/latest").json()) == {
        "started_at", "finished_at", "status", "postings_fetched", "postings_new",
        "postings_scored", "input_tokens", "output_tokens",
    }  # fmt: skip


def test_matches_filter_order_and_latest_resume_only(client, db):
    old = add_resume(db)
    add_match(db, old, title="OLD", score=99)
    new = add_resume(db)
    add_match(db, new, title="LOW", score=20)
    add_match(db, new, title="HIGH", score=90)
    add_match(db, new, title="MID", score=50)
    got = client.get("/matches", headers=AUTH).json()
    assert [m["title"] for m in got] == ["HIGH", "MID", "LOW"]
    assert set(got[0]) == {"id", "score", "summary", "company", "title", "url"}
    assert [m["title"] for m in client.get("/matches?min_score=50", headers=AUTH).json()] == [
        "HIGH",
        "MID",
    ]
    assert len(client.get("/matches?limit=1", headers=AUTH).json()) == 1
    for q in ("min_score=101", "min_score=-1", "limit=0", "limit=201"):
        assert client.get(f"/matches?{q}", headers=AUTH).status_code == 422


def test_match_detail_and_404(client, db):
    mid = add_match(db, add_resume(db))
    r = client.get(f"/matches/{mid}", headers=AUTH).json()
    assert r["summary"] == "SUMMARY-TEXT" and r["gaps"] == ["GAP-TEXT"] and r["model"] == "m"
    assert r["reasons"][0]["evidence"] == "EVIDENCE-TEXT"
    assert {"score", "scored_at", "company", "title", "locations", "url"} <= set(r)
    assert client.get("/matches/999", headers=AUTH).status_code == 404


def test_page_empty_state(client, db):
    r = client.get("/")
    assert r.status_code == 200 and "No runs yet" in r.text and "No matches yet" in r.text
    assert "<title>Internship Scout</title>" in r.text


def test_page_public_view(client, db):
    rid = add_resume(db)
    add_match(db, rid, title="Rust Intern", score=77)
    db.execute("INSERT INTO runs (status, finished_at, postings_scored) VALUES ('succeeded', now(), 1)")
    r = client.get("/")
    assert r.headers["content-security-policy"] == main.CSP
    for shown in ("Acme", "Rust Intern", "77", "succeeded", 'href="https://example.com/p"'):
        assert shown in r.text
    for hidden in ("SUMMARY-TEXT", "EVIDENCE-TEXT", "GAP-TEXT"):
        assert hidden not in r.text
    assert "innerHTML" not in r.text


def test_page_escapes_and_only_links_https(client, db):
    rid = add_resume(db)
    add_match(db, rid, title="<script>alert(1)</script>", url='https://x.com/"onmouseover="a')
    add_match(db, rid, title="Plain", url="javascript:alert(1)", score=10)
    text = client.get("/").text
    assert "<script>alert(1)</script>" not in text and "&lt;script&gt;alert(1)" in text
    assert 'href="https://x.com/&quot;onmouseover=&quot;a"' in text
    assert "javascript:alert" not in text and "Plain" in text
