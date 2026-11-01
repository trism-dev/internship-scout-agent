import base64
import hashlib
import hmac
import os
from contextlib import asynccontextmanager
from datetime import UTC
from html import escape

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.db import apply_schema, connect
from app.demo_data import RESUME, SAMPLES

MAX_RESUME = 50_000

RUN_COLUMNS = (
    "started_at, finished_at, status, postings_fetched, postings_new, postings_scored,"
    " input_tokens, output_tokens, digest_sent"
)

MATCHES = """
SELECT m.id, m.score, m.summary, p.company, p.title, p.url
FROM matches m JOIN postings p ON p.id = m.posting_id
WHERE m.resume_id = (SELECT max(id) FROM resumes) AND m.score >= %s AND p.active
ORDER BY m.score DESC, m.id LIMIT %s
"""

DETAIL = """
SELECT m.score, m.summary, m.reasons, m.gaps, m.model, m.scored_at,
       p.company, p.title, p.locations, p.url
FROM matches m JOIN postings p ON p.id = m.posting_id WHERE m.id = %s
"""

CSS = """
:root { color-scheme: light dark; }
body { font: 16px/1.5 system-ui, sans-serif; max-width: 52rem; margin: 0 auto; padding: 0 16px 2rem; }
table { border-collapse: collapse; width: 100%; }
th { white-space: nowrap; }
th, td { text-align: left; padding: .4rem .5rem; border-bottom: 1px solid #8884; vertical-align: top;
         overflow-wrap: anywhere; }
input { font: inherit; max-width: 100%; }
.x p, .x ul { margin: .4rem 0; }
"""

JS = """
const token = document.getElementById('token');
document.addEventListener('click', async (e) => {
  const b = e.target.closest('button[data-id]');
  if (!b) return;
  const box = b.nextElementSibling;
  box.replaceChildren();
  const add = (tag, text, parent = box) => {
    const el = document.createElement(tag);
    el.textContent = text;
    parent.append(el);
    return el;
  };
  try {
    const r = await fetch('/matches/' + b.dataset.id,
      { headers: { Authorization: 'Bearer ' + token.value } });
    if (!r.ok) return add('p', r.status === 401 ? 'Enter a valid token.' : 'Error ' + r.status);
    const m = await r.json();
    add('p', m.summary);
    const reasons = add('ul', '');
    for (const x of m.reasons) add('li', x.requirement + ': ' + x.evidence, reasons);
    if (m.gaps.length) add('p', 'Gaps');
    const gaps = m.gaps.length ? add('ul', '') : null;
    for (const g of m.gaps) add('li', g, gaps);
  } catch {
    add('p', 'Request failed.');
  }
});
"""

JS_HASH = base64.b64encode(hashlib.sha256(JS.encode()).digest()).decode()
DEMO_CSP = "default-src 'none'; style-src 'unsafe-inline'"
CSP = (
    f"default-src 'none'; script-src 'sha256-{JS_HASH}'; style-src 'unsafe-inline'; "
    "connect-src 'self'"
)


@asynccontextmanager
async def lifespan(app):
    with connect() as conn:
        apply_schema(conn)
    yield


app = FastAPI(lifespan=lifespan)


def auth(authorization: str = Header(default="")):
    token = os.environ.get("API_TOKEN", "")
    if not token:
        raise HTTPException(503, "API_TOKEN is not configured")
    if not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, "invalid token")


class ResumeIn(BaseModel):
    text: str


@app.get("/health")
def health():
    try:
        with connect() as conn:
            conn.execute("SELECT 1")
    except psycopg.Error:
        return JSONResponse({"status": "db unavailable"}, status_code=503)
    return {"status": "ok"}


@app.put("/resume", dependencies=[Depends(auth)])
def put_resume(body: ResumeIn):
    text = body.text.strip()
    if not text or len(text) > MAX_RESUME or "\x00" in text:
        raise HTTPException(422, "resume must be 1 to 50000 characters without NUL")
    with connect() as conn:
        row = conn.execute("INSERT INTO resumes (text) VALUES (%s) RETURNING id", (text,))
        return {"id": row.fetchone()["id"]}


def latest_run(conn):
    return conn.execute(f"SELECT {RUN_COLUMNS} FROM runs ORDER BY id DESC LIMIT 1").fetchone()


@app.get("/runs/latest")
def get_latest_run():
    with connect() as conn:
        run = latest_run(conn)
    if not run:
        raise HTTPException(404, "no runs")
    return run


@app.get("/matches", dependencies=[Depends(auth)])
def get_matches(min_score: int = Query(0, ge=0, le=100), limit: int = Query(50, ge=1, le=200)):
    with connect() as conn:
        return conn.execute(MATCHES, (min_score, limit)).fetchall()


@app.get("/matches/{match_id}", dependencies=[Depends(auth)])
def get_match(match_id: int):
    with connect() as conn:
        row = conn.execute(DETAIL, (match_id,)).fetchone()
    if not row:
        raise HTTPException(404, "no such match")
    return row


def table_row(m):
    title = escape(m["title"])
    if m["url"].startswith("https://"):
        title = f'<a href="{escape(m["url"])}" rel="noopener noreferrer">{title}</a>'
    return (
        f"<tr><td>{escape(m['company'])}</td><td>{title}</td><td>{m['score']}</td>"
        f'<td><button type="button" data-id="{m["id"]}">Explain</button><div class="x"></div></td></tr>'
    )


def layout(body):
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Internship Scout</title>
<style>{CSS}</style>
</head>
<body>
{body}
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    with connect() as conn:
        run = latest_run(conn)
        top = conn.execute(MATCHES, (0, 10)).fetchall()
    if run:
        done = run["finished_at"].astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if run["finished_at"] else "not yet"
        stats = (
            f"<p>Last run: {escape(run['status'])}, finished {done}, "
            f"{run['postings_scored']} postings scored.</p>"
        )
    else:
        stats = "<p>No runs yet.</p>"
    if top:
        rows = "".join(map(table_row, top))
        matches = (
            "<table><thead><tr><th>Company</th><th>Title</th><th>Score</th><th></th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    else:
        matches = "<p>No matches yet.</p>"
    body = f"""<h1>Internship Scout</h1>
{stats}
<p>This table is the owner's real run. <a href="/demo">See a full example with sample data.</a></p>
<h2>Top matches</h2>
{matches}
<p><label>API token to explain a match <input id="token" type="password" autocomplete="off"></label></p>
<script>{JS}</script>"""
    return HTMLResponse(layout(body), headers={"Content-Security-Policy": CSP})


def demo_match(company, title, score, summary, reasons, gaps):
    reason_items = "".join(f"<li>{escape(a)}: {escape(b)}</li>" for a, b in reasons)
    reasons_html = f"<ul>{reason_items}</ul>" if reasons else "<p>No matching requirements.</p>"
    gap_items = "".join(f"<li>{escape(g)}</li>" for g in gaps)
    gaps_html = f"<p>Gaps</p><ul>{gap_items}</ul>" if gaps else ""
    return (
        f"<section class=\"x\"><h3>{escape(company)}: {escape(title)} ({score})</h3>"
        f"<p>{escape(summary)}</p>{reasons_html}{gaps_html}</section>"
    )


@app.get("/demo", response_class=HTMLResponse)
def demo():
    body = f"""<h1>Internship Scout demo</h1>
<p><strong>Sample data: fictional resume and companies.</strong> Nothing here comes from a real run.</p>
<p><a href="/">Back to the live table</a></p>
<h2>Sample resume</h2>
<p>{escape(RESUME)}</p>
<h2>Sample matches</h2>
{"".join(demo_match(*s) for s in SAMPLES)}"""
    return HTMLResponse(layout(body), headers={"Content-Security-Policy": DEMO_CSP})
