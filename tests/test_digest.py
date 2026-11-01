import itertools
import json
import tempfile
from pathlib import Path

import httpx
import pytest

from app import digest

COUNT = itertools.count(1)
ENV = {"RESEND_API_KEY": "re_test", "DIGEST_FROM": "Scout <a@example.com>", "DIGEST_TO": "me@example.com"}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for k in (*ENV, "DIGEST_DRY_RUN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


def configured(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


def resume(db):
    return db.execute("INSERT INTO resumes (text) VALUES ('r') RETURNING id").fetchone()["id"]


def add(db, rid, score=80, title="Intern", url="https://example.com/p", active=True, **kw):
    pid = db.execute(
        "INSERT INTO postings (source, external_id, company, title, url, active)"
        " VALUES ('s', %s, 'Acme', %s, %s, %s) RETURNING id",
        (str(next(COUNT)), title, url, active),
    ).fetchone()["id"]
    cols = {"summary": "sum", "reasons": '[{"requirement": "r", "evidence": "e"}]', "gaps": '["g"]'} | kw
    return db.execute(
        "INSERT INTO matches (posting_id, resume_id, score, summary, reasons, gaps, model)"
        " VALUES (%s, %s, %s, %s, %s, %s, 'm') RETURNING id",
        (pid, rid, score, cols["summary"], cols["reasons"], cols["gaps"]),
    ).fetchone()["id"]


def mail(captured, status=200):
    def handler(request):
        captured.append(request)
        return httpx.Response(status, json={"id": "x"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def digested(db):
    rows = db.execute("SELECT id FROM matches WHERE digested_at IS NOT NULL ORDER BY id").fetchall()
    return [r["id"] for r in rows]


def test_select_threshold_order_and_exclusions(db):
    rid, other = resume(db), resume(db)
    low = add(db, rid, score=69)
    edge = add(db, rid, score=70)
    top = add(db, rid, score=95)
    add(db, rid, score=99, active=False)
    done = add(db, rid, score=99)
    db.execute("UPDATE matches SET digested_at = now() WHERE id = %s", (done,))
    add(db, other, score=99)
    got = digest.select_matches(db, rid)
    assert [m["id"] for m in got] == [top, edge] and low not in [m["id"] for m in got]
    assert {"company", "title", "url", "locations", "summary", "reasons", "gaps"} <= set(got[0])


def test_select_caps_and_keeps_best(db):
    rid = resume(db)
    for i in range(20):
        add(db, rid, score=70 + i)
    got = digest.select_matches(db, rid)
    assert len(got) == digest.MAX_MATCHES and got[0]["score"] == 89 and got[-1]["score"] == 75


def row(**kw):
    base = {"id": 1, "score": 88, "company": "Acme", "title": "Intern", "url": "https://x.com/p",
            "summary": "S", "reasons": [{"requirement": "R", "evidence": "E"}], "gaps": ["G"],
            "locations": []}  # fmt: skip
    return base | kw


def test_render_escapes_and_links_only_https():
    bad = "<script>alert(1)</script>"
    subject, html, text = digest.render(
        [
            row(title=bad, summary=bad, reasons=[{"requirement": "R", "evidence": bad}], gaps=[bad]),
            row(title="Plain", url="javascript:alert(1)"),
            row(title="Http", url="http://x.com"),
        ],
        42,
    )
    assert "<script>" not in html and html.count("&lt;script&gt;alert(1)") >= 4
    assert 'href="https://x.com/p"' in html and "javascript:" not in html and 'href="http://' not in html
    assert "Internship Scout: 3 matches" in subject and "LLM estimates" in html and "42" in html
    assert "<b>88</b> &middot; Acme" in html and "88 - Acme - Plain" in text
    assert "  + R: E" in text and "  - gap: G" in text
    assert digest.render([row()], 1)[0] == "Internship Scout: 1 match"


def test_href_quote_breakout_is_escaped():
    _, html, _ = digest.render([row(url='https://x.com/"onmouseover="a')], 1)
    assert 'href="https://x.com/&quot;onmouseover=&quot;a"' in html and '"onmouseover=' not in html


def test_idempotency_key_stable_per_ids():
    k = digest.idempotency_key([3, 1, 2])
    assert k == digest.idempotency_key([1, 2, 3]) != digest.idempotency_key([1, 2, 4])


def test_send_request_shape(monkeypatch):
    configured(monkeypatch)
    reqs = []
    digest.send("subj", "<p>h</p>", "t", mail(reqs), "k1")
    r = reqs[0]
    assert (r.method, str(r.url)) == ("POST", "https://api.resend.com/emails")
    assert r.headers["authorization"] == "Bearer re_test" and r.headers["idempotency-key"] == "k1"
    assert json.loads(r.content) == {
        "from": "Scout <a@example.com>", "to": ["me@example.com"], "subject": "subj",
        "html": "<p>h</p>", "text": "t",
    }  # fmt: skip


def test_deliver_unconfigured_writes_file_and_marks_nothing(db, monkeypatch):
    rid = resume(db)
    add(db, rid, title="FileTitle")
    reqs = []
    assert digest.deliver(db, rid, 1, mail(reqs)) is False
    files = list(Path(tempfile.gettempdir()).glob("internship-scout-digest-*.html"))
    assert len(files) == 1 and "FileTitle" in files[0].read_text(encoding="utf-8")
    assert reqs == [] and digested(db) == []
    configured(monkeypatch)
    monkeypatch.setenv("DIGEST_DRY_RUN", "1")
    assert digest.deliver(db, rid, 1, mail(reqs)) is False
    assert reqs == [] and digested(db) == []


def test_deliver_marks_only_sent_ids_and_second_run_sends_nothing(db, monkeypatch):
    configured(monkeypatch)
    rid = resume(db)
    a, b = add(db, rid, score=90), add(db, rid, score=80)
    skipped = add(db, rid, score=10)
    reqs = []
    assert digest.deliver(db, rid, 3, mail(reqs)) is True
    assert digested(db) == [a, b] and skipped not in digested(db) and len(reqs) == 1
    assert reqs[0].headers["idempotency-key"] == digest.idempotency_key([a, b])
    assert digest.deliver(db, rid, 3, mail(reqs)) is False
    assert len(reqs) == 1


def test_deliver_bad_status_raises_and_marks_nothing(db, monkeypatch):
    configured(monkeypatch)
    rid = resume(db)
    add(db, rid)
    with pytest.raises(httpx.HTTPStatusError):
        digest.deliver(db, rid, 1, mail([], 500))
    assert digested(db) == []


def test_deliver_no_matches_sends_nothing(db, monkeypatch):
    configured(monkeypatch)
    rid = resume(db)
    add(db, rid, score=10)
    reqs = []
    assert digest.deliver(db, rid, 1, mail(reqs)) is False
    assert reqs == [] and not list(Path(tempfile.gettempdir()).glob("internship-scout-digest-*"))
