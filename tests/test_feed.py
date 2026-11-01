from datetime import UTC, datetime

import httpx
import pytest

from app import feed
from app.feed import fetch_listings, parse_listings, upsert_postings

NOW = datetime(2026, 11, 1, tzinfo=UTC)
RECENT = NOW.timestamp() - 86400


def entry(id, **kw):
    e = {
        "id": id,
        "company_name": "Acme",
        "title": "SWE Intern",
        "url": "https://x.com/" + id,
        "locations": ["NYC"],
        "terms": ["Summer 2027"],
        "category": "Software",
        "sponsorship": "Other",
        "active": True,
        "is_visible": True,
        "date_posted": RECENT,
    }
    return e | kw


def test_parse_filters_and_skips():
    raw = [
        entry("ok"),
        entry("noflag", is_visible=None) | {},
        entry("hidden", is_visible=False),
        entry("http", url="http://x.com"),
        entry("old", date_posted=NOW.timestamp() - 8 * 86400),
        entry("gone", active=False, date_posted=0),
        entry("badtype", title=5),
        entry("baddate", date_posted="yesterday"),
        entry("boolpost", date_posted=True),
        entry("huge", date_posted=1e30),
        {k: v for k, v in entry("notitle").items() if k != "title"},
        "junk",
        None,
    ]
    del raw[1]["is_visible"]
    postings, inactive, skipped = parse_listings(raw, NOW)
    assert [p["external_id"] for p in postings] == ["ok", "noflag"]
    assert inactive == ["gone"]
    assert skipped == 7


def test_upsert_insert_update_and_deactivate(db):
    postings, _, _ = parse_listings([entry("a"), entry("b")], NOW)
    assert upsert_postings(db, postings, []) == 2
    assert upsert_postings(db, postings, []) == 0
    db.execute("UPDATE postings SET description = 'keep' WHERE external_id = 'a'")
    changed = [dict(postings[0], title="New title")]
    assert upsert_postings(db, changed, []) == 0
    row = db.execute("SELECT title, description FROM postings WHERE external_id = 'a'").fetchone()
    assert row == {"title": "New title", "description": "keep"}
    assert upsert_postings(db, [], ["b", "unknown"]) == 0
    rows = db.execute("SELECT external_id, active FROM postings ORDER BY external_id").fetchall()
    assert rows == [
        {"external_id": "a", "active": True},
        {"external_id": "b", "active": False},
    ]


def test_fetch_listings():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[1])))
    assert fetch_listings(client) == [1]
    bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_listings(bad)


def test_fetch_listings_default_client(monkeypatch):
    monkeypatch.setattr(
        feed.httpx,
        "get",
        lambda *a, **k: httpx.Response(200, json=[2], request=httpx.Request("GET", "x")),
    )
    assert fetch_listings() == [2]
