import json
import socket

import anthropic
import httpx
import httpx2
import pytest

from app import agent
from app.agent import fetch_text, score_postings

DNS = {"example.com": "93.184.216.34", "evil.com": "10.0.0.5"}
HTML = b"<html><style>p{}</style><script>alert(1)</script><p>Build  <b>things</b></p></html>"
ERR = {"type": "error", "error": {"type": "api_error", "message": "boom"}}


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    def fake(host, port, **kw):
        if host not in DNS and host.replace(".", "").isdigit():
            return [(2, 1, 6, "", (host, port))]
        if host not in DNS:
            raise socket.gaierror(host)
        return [(2, 1, 6, "", (DNS[host], port))]

    monkeypatch.setattr(agent.socket, "getaddrinfo", fake)


def web(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok_html(request):
    return httpx.Response(200, content=HTML, headers={"content-type": "text/html; charset=utf-8"})


def msg(blocks, stop="tool_use", i=10, o=5, **usage):
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "m", "content": blocks,
        "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": i, "output_tokens": o, **usage},
    }  # fmt: skip


def end():
    return msg([{"type": "text", "text": "done"}], "end_turn")


def use(name, n=1, **inp):
    return {"type": "tool_use", "id": f"toolu_{name}{n}", "name": name, "input": inp}


def rec(pid, score=80, **kw):
    inp = {"posting_id": pid, "score": score, "summary": "ok", "reasons": [], "gaps": ["x"]}
    return use("record_match", pid, **(inp | kw))


def client(*responses):
    reqs, it = [], iter(responses)

    def handler(request):
        reqs.append(json.loads(request.content))
        r = next(it)
        return r if isinstance(r, httpx2.Response) else httpx2.Response(200, json=r)

    http = httpx2.Client(transport=httpx2.MockTransport(handler))
    return anthropic.Anthropic(api_key="test", max_retries=0, http_client=http), reqs


def seed(db, n, description=None):
    db.execute("INSERT INTO resumes (text) VALUES ('r')")
    for i in range(n):
        db.execute(
            "INSERT INTO postings (source, external_id, company, title, url, posted_at, description)"
            " VALUES ('s', %s, 'Acme', %s, 'https://example.com/p', now() - %s * interval '1 day', %s)",
            (str(i), f"T{i}", i, description),
        )


def matches(db):
    return {r["posting_id"]: r for r in db.execute("SELECT * FROM matches ORDER BY posting_id")}


def results(req):
    return req["messages"][-1]["content"]


def test_happy_path_and_request_shape(db):
    seed(db, 2)
    first = msg([use("get_posting_details", 1, posting_id=1), rec(2)], i=10, o=5)
    c, reqs = client(first, msg([rec(1)], i=20, o=6, cache_read_input_tokens=5), end())
    out = score_postings(db, c, 1, "I know Rust.", run_id=None, http=web(ok_html))
    assert out == {"scored": 2, "input_tokens": 10 + 25 + 10, "output_tokens": 5 + 6 + 5,
                   "failed_batches": 0}  # fmt: skip
    assert (
        db.execute("SELECT description FROM postings WHERE id = 1").fetchone()["description"]
        == "Build things"
    )
    m = matches(db)
    assert m[1]["score"] == 80 and m[1]["model"] == agent.MODEL and m[1]["gaps"] == ["x"]

    r0 = reqs[0]
    assert r0["tool_choice"] == {"type": "auto"}
    assert r0["output_config"] == {"effort": "low"}
    assert "temperature" not in r0 and "thinking" not in r0
    assert "I know Rust." in r0["system"]
    assert next(t for t in r0["tools"] if t["name"] == "record_match")["strict"] is True
    assert [p["id"] for p in json.loads(r0["messages"][0]["content"].split("\n", 1)[1])] == [1, 2]

    assistant = reqs[1]["messages"][1]
    assert assistant["role"] == "assistant"
    assert [(b["type"], b["id"], b["input"]) for b in assistant["content"]] == [
        (b["type"], b["id"], b["input"]) for b in first["content"]
    ]
    last = reqs[1]["messages"][2]
    assert last["role"] == "user" and len(reqs[1]["messages"]) == 3
    assert [b["tool_use_id"] for b in last["content"]] == [b["id"] for b in first["content"]]
    assert not any(b["is_error"] for b in last["content"])


def test_invalid_record_match_is_error_and_saves_nothing(db):
    seed(db, 1)
    c, reqs = client(
        msg([rec(99), rec(1, 101)]),
        msg([rec(1, summary="a\x00b")]),
        end(),
        end(),
    )
    out = score_postings(db, c, 1, "r", http=web(ok_html))
    assert [b["is_error"] for b in results(reqs[1])] == [True, True]
    assert results(reqs[2])[0]["is_error"] is True and "save" in results(reqs[2])[0]["content"]
    assert matches(db) == {} and out["scored"] == 0 and out["failed_batches"] == 0


def test_unscored_gets_one_reprompt_then_next_run_picks_it_up(db):
    seed(db, 2)
    c, reqs = client(msg([rec(1)]), end(), end())
    out = score_postings(db, c, 1, "r", http=web(ok_html))
    assert out["scored"] == 1 and out["failed_batches"] == 0 and len(reqs) == 3
    assert "[2]" in reqs[2]["messages"][-1]["content"]
    assert set(matches(db)) == {1}
    before = matches(db)[1]["scored_at"]

    c, reqs = client(msg([rec(2, 55)]), end())
    out = score_postings(db, c, 1, "r", http=web(ok_html))
    assert out["scored"] == 1
    assert [p["id"] for p in json.loads(reqs[0]["messages"][0]["content"].split("\n", 1)[1])] == [2]
    m = matches(db)
    assert set(m) == {1, 2} and m[1]["scored_at"] == before and m[2]["score"] == 55


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_bad_stop_keeps_scores_and_counts_failed_batch(db, monkeypatch, stop):
    monkeypatch.setattr(agent, "BATCH_SIZE", 1)
    seed(db, 2)
    c, reqs = client(msg([rec(1)]), msg([], stop), msg([rec(2)]), end())
    out = score_postings(db, c, 1, "r")
    assert out["failed_batches"] == 1 and out["scored"] == 2 and set(matches(db)) == {1, 2}
    assert len(reqs) == 4


def test_server_error_fails_batch_next_batch_still_runs(db, monkeypatch):
    monkeypatch.setattr(agent, "BATCH_SIZE", 1)
    seed(db, 2)
    c, _ = client(httpx2.Response(500, json=ERR), msg([rec(2)]), end())
    out = score_postings(db, c, 1, "r")
    assert out["failed_batches"] == 1 and out["scored"] == 1 and set(matches(db)) == {2}


def test_auth_error_raises(db):
    seed(db, 1)
    err = {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}
    c, _ = client(httpx2.Response(401, json=err))
    with pytest.raises(anthropic.AuthenticationError):
        score_postings(db, c, 1, "r")


def test_max_turns_stops_loop(db, monkeypatch):
    monkeypatch.setattr(agent, "MAX_TURNS", 2)
    seed(db, 1)
    c, reqs = client(msg([use("nope")]), msg([use("nope")]), msg([use("nope")]))
    out = score_postings(db, c, 1, "r")
    assert len(reqs) == 2 and out["failed_batches"] == 1 and out["scored"] == 0


def test_stored_description_is_not_fetched(db):
    seed(db, 1, description="stored text")

    def boom(request):
        raise AssertionError("fetched")

    c, reqs = client(msg([use("get_posting_details", 1, posting_id=1)]), msg([rec(1)]), end())
    score_postings(db, c, 1, "r", http=web(boom))
    assert results(reqs[1])[0]["content"] == "stored text"


def test_details_errors(db):
    seed(db, 1)
    c, reqs = client(
        msg([use("get_posting_details", 1, posting_id=7)]),
        msg([use("get_posting_details", 2, posting_id=1)]),
        msg([rec(1)]),
        end(),
    )
    score_postings(db, c, 1, "r", http=web(lambda request: httpx.Response(500)))
    assert results(reqs[1])[0]["is_error"] is True
    assert results(reqs[2])[0]["is_error"] is True
    assert db.execute("SELECT description FROM postings").fetchone()["description"] is None
    assert set(matches(db)) == {1}


@pytest.mark.parametrize("url", ["http://example.com/", "ftp://example.com/", "https:///x"])
def test_fetch_refuses_non_https(url):
    with pytest.raises(ValueError):
        fetch_text(web(ok_html), url)


@pytest.mark.parametrize("host", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "evil.com"])
def test_fetch_refuses_private_hosts(host):
    calls = []
    with pytest.raises(ValueError, match="public"):
        fetch_text(web(lambda r: calls.append(r) or ok_html(r)), f"https://{host}/")
    assert calls == []


def test_fetch_refuses_redirect_to_private_host():
    calls = []

    def h(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://evil.com/x"})

    with pytest.raises(ValueError, match="public"):
        fetch_text(web(h), "https://example.com/")
    assert len(calls) == 1


def test_fetch_refuses_too_many_redirects():
    calls = []

    def h(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "/next"})

    with pytest.raises(ValueError, match="redirects"):
        fetch_text(web(h), "https://example.com/")
    assert len(calls) == agent.MAX_HOPS + 1


def test_fetch_follows_relative_redirect_and_strips_html():
    def h(request):
        if request.url.path == "/a":
            return httpx.Response(301, headers={"location": "/b"})
        return ok_html(request)

    assert fetch_text(web(h), "https://example.com/a") == "Build things"


def test_fetch_truncates_text_and_caps_body():
    body = b"x" * (agent.MAX_BYTES + 10)
    h = lambda r: httpx.Response(200, content=body, headers={"content-type": "text/plain"})
    assert len(fetch_text(web(h), "https://example.com/")) == agent.MAX_CHARS


def test_fetch_refuses_non_html():
    h = lambda r: httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})
    with pytest.raises(ValueError, match="html"):
        fetch_text(web(h), "https://example.com/")


def test_invalid_url_slow_fetch_and_bool_id(db, monkeypatch):
    with pytest.raises(httpx.InvalidURL):
        fetch_text(web(ok_html), "https://[::1/")

    ticks = iter(range(0, 1000, 40))
    monkeypatch.setattr(agent.time, "monotonic", lambda: next(ticks))
    h = lambda r: httpx.Response(200, content=b"abc", headers={"content-type": "text/plain"})
    with pytest.raises(ValueError, match="slow"):
        fetch_text(web(h), "https://example.com/")

    seed(db, 1)
    c, reqs = client(msg([rec(True)]), end(), end())
    score_postings(db, c, 1, "r")
    assert results(reqs[1])[0]["is_error"] is True and matches(db) == {}
