import logging
import time
from types import SimpleNamespace

import httpx
import psycopg
import pytest
import test_agent as ta
from fastapi.testclient import TestClient
from test_api import add_resume

from app import agent, run
from app.main import app


@pytest.fixture(autouse=True)
def no_digest(monkeypatch):
    calls = []
    monkeypatch.setattr(run, "deliver", lambda *a: calls.append(a) or False)
    return calls


def feed(*ids):
    return [
        {"id": i, "active": True, "company_name": "Acme", "title": f"T{i}", "date_posted": time.time() - n,
         "url": f"https://example.com/{i}", "locations": [], "terms": [], "category": "SWE",
         "sponsorship": "Other"}
        for n, i in enumerate(ids)
    ]  # fmt: skip


def use_anthropic(monkeypatch, *responses):
    c, reqs = ta.client(*responses)
    monkeypatch.setattr(run, "anthropic", SimpleNamespace(Anthropic=lambda: c))
    return reqs


def exit_code():
    try:
        run.main()
    except SystemExit as e:
        return e.code
    return 0


def test_no_resume(db):
    assert exit_code() == "no resume"
    assert db.execute("SELECT count(*) AS n FROM runs").fetchone()["n"] == 0


def test_run_conflicts(db):
    add_resume(db)
    db.execute("INSERT INTO runs DEFAULT VALUES")
    assert exit_code() == "a run is already running"
    assert db.execute("SELECT count(*) AS n FROM runs").fetchone()["n"] == 1


def test_latest_resume_is_scored(db, monkeypatch):
    add_resume(db, "first resume")
    add_resume(db, "second resume")
    monkeypatch.setattr(run, "fetch_listings", lambda: feed("a"))
    reqs = use_anthropic(monkeypatch, ta.msg([ta.rec(1)]), ta.end())
    assert exit_code() == 0
    assert "second resume" in reqs[0]["system"] and "first resume" not in reqs[0]["system"]
    assert db.execute("SELECT resume_id FROM matches").fetchone()["resume_id"] == 2


def test_full_run_then_rerun_scores_nothing_twice(db, monkeypatch, capsys):
    add_resume(db)
    monkeypatch.setattr(run, "fetch_listings", lambda: feed("a", "b"))
    reqs = use_anthropic(monkeypatch, ta.msg([ta.rec(1, 90), ta.rec(2, 40)], i=10, o=5), ta.end())
    assert exit_code() == 0
    assert "Fictional" in reqs[0]["system"]
    out = capsys.readouterr().out
    assert "run 1: status=succeeded" in out and "postings_scored=2" in out
    assert "Fictional" not in out
    row = db.execute("SELECT * FROM runs").fetchone()
    assert row["status"] == "succeeded" and row["finished_at"] is not None
    assert (row["postings_fetched"], row["postings_new"], row["postings_scored"]) == (2, 2, 2)
    assert (row["input_tokens"], row["output_tokens"], row["digest_sent"]) == (20, 10, False)

    use_anthropic(monkeypatch)
    assert exit_code() == 0
    row = db.execute("SELECT * FROM runs WHERE id = 2").fetchone()
    assert (row["postings_fetched"], row["postings_new"], row["postings_scored"]) == (2, 0, 0)
    assert row["input_tokens"] == 0 and row["status"] == "succeeded"
    assert db.execute("SELECT count(*) AS n FROM matches").fetchone()["n"] == 2


def test_all_batches_failed_is_failed_but_partial_is_succeeded(db, monkeypatch):
    add_resume(db)
    monkeypatch.setattr(run, "fetch_listings", lambda: feed("a"))
    use_anthropic(monkeypatch, ta.msg([], "refusal"))
    assert exit_code() == 1
    row = db.execute("SELECT status, error, finished_at FROM runs").fetchone()
    assert (row["status"], row["error"]) == ("failed", "AllBatchesFailed") and row["finished_at"]

    monkeypatch.setattr(agent, "BATCH_SIZE", 1)
    monkeypatch.setattr(run, "fetch_listings", lambda: feed("a", "b"))
    use_anthropic(monkeypatch, ta.msg([ta.rec(1)]), ta.end(), ta.msg([], "refusal"))
    assert exit_code() == 0
    row = db.execute("SELECT status, error, postings_scored FROM runs ORDER BY id DESC").fetchone()
    assert (row["status"], row["error"], row["postings_scored"]) == ("succeeded", None, 1)


def run_once(db, monkeypatch, *responses):
    add_resume(db)
    monkeypatch.setattr(run, "fetch_listings", lambda: feed("a"))
    use_anthropic(monkeypatch, *responses)
    code = exit_code()
    return code, db.execute("SELECT status, digest_sent FROM runs").fetchone()


def test_digest_sent_flag_set_on_success(db, monkeypatch, no_digest):
    monkeypatch.setattr(run, "deliver", lambda *a: no_digest.append(a) or True)
    code, row = run_once(db, monkeypatch, ta.msg([ta.rec(1)]), ta.end())
    assert (code, row["status"], row["digest_sent"]) == (0, "succeeded", True)
    assert no_digest[0][1:] == (1, 1)


def test_digest_failure_keeps_run_succeeded(db, monkeypatch):
    def boom(*a):
        raise httpx.ConnectError("sk-secret-value")

    monkeypatch.setattr(run, "deliver", boom)
    code, row = run_once(db, monkeypatch, ta.msg([ta.rec(1)]), ta.end())
    assert (code, row["status"], row["digest_sent"]) == (0, "succeeded", False)


def test_any_digest_error_keeps_run_succeeded(db, monkeypatch):
    def boom(*a):
        raise OSError("secret")

    monkeypatch.setattr(run, "deliver", boom)
    code, row = run_once(db, monkeypatch, ta.msg([ta.rec(1)]), ta.end())
    assert (code, row["status"], row["digest_sent"]) == (0, "succeeded", False)


def test_failed_run_does_not_deliver(db, monkeypatch, no_digest):
    code, row = run_once(db, monkeypatch, ta.msg([], "refusal"))
    assert code == 1 and row["status"] == "failed" and no_digest == []


def test_failure_write_failing_does_not_raise(db, monkeypatch):
    add_resume(db)
    db.execute("INSERT INTO runs DEFAULT VALUES")
    real, calls = run.connect, []

    def flaky():
        calls.append(1)
        if len(calls) > 1:
            raise psycopg.OperationalError("down")
        return real()

    def boom():
        raise RuntimeError

    monkeypatch.setattr(run, "connect", flaky)
    monkeypatch.setattr(run, "fetch_listings", boom)
    run.run_pipeline(1)
    assert len(calls) == 2
    assert db.execute("SELECT status FROM runs").fetchone()["status"] == "running"


def test_stale_running_row_is_failed_and_new_run_starts(db, monkeypatch):
    add_resume(db)
    db.execute("INSERT INTO runs (started_at) VALUES (now() - interval '7 hours')")
    monkeypatch.setattr(run, "fetch_listings", list)
    use_anthropic(monkeypatch)
    assert exit_code() == 0
    old = db.execute("SELECT status, error, finished_at FROM runs WHERE id = 1").fetchone()
    assert (old["status"], old["error"]) == ("failed", "stale") and old["finished_at"]
    assert db.execute("SELECT status FROM runs WHERE id = 2").fetchone()["status"] == "succeeded"


def test_recent_running_row_is_not_stale(db):
    add_resume(db)
    db.execute("INSERT INTO runs (started_at) VALUES (now() - interval '5 hours')")
    assert exit_code() == "a run is already running"
    assert db.execute("SELECT status FROM runs").fetchone()["status"] == "running"


def test_pipeline_failure_stores_and_logs_only_class_name(db, monkeypatch, caplog):
    add_resume(db)

    def boom():
        raise RuntimeError("sk-secret-" + "value")  # traceback frames print source lines

    monkeypatch.setattr(run, "fetch_listings", boom)
    with caplog.at_level(logging.INFO):
        assert exit_code() == 1
    row = db.execute("SELECT status, error, finished_at FROM runs").fetchone()
    assert row["status"] == "failed" and row["error"] == "RuntimeError" and row["finished_at"]
    assert "RuntimeError" in caplog.text and "sk-secret-value" not in caplog.text
    r = TestClient(app).get("/runs/latest")
    assert "error" not in r.json() and "secret" not in r.text and "RuntimeError" not in r.text
