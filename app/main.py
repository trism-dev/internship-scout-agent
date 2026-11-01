import base64
import hashlib
import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from html import escape

import anthropic
import psycopg
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.agent import score_postings
from app.db import apply_schema, connect
from app.feed import fetch_listings, parse_listings, upsert_postings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

MAX_RESUME = 50_000

RUN_COLUMNS = (
    "started_at, finished_at, status, postings_fetched, postings_new, postings_scored,"
    " input_tokens, output_tokens"
)

MATCHES = """
SELECT m.id, m.score, m.summary, p.company, p.title, p.url
FROM matches m JOIN postings p ON p.id = m.posting_id
WHERE m.resume_id = (SELECT max(id) FROM resumes) AND m.score >= %s
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
CSP = (
    f"default-src 'none'; script-src 'sha256-{JS_HASH}'; style-src 'unsafe-inline'; "
    "connect-src 'self'"
)


@asynccontextmanager
async def lifespan(app):
    with connect() as conn:
        apply_schema(conn)
        # ponytail: single instance only; a second instance would fail the first one's live run
        conn.execute(
            "UPDATE runs SET status = 'failed', finished_at = now(), error = 'interrupted'"
            " WHERE status = 'running'"
        )
    yield


app = FastAPI(lifespan=lifespan)


def auth(authorization: str = Header(default="")):
    token = os.environ.get("API_TOKEN", "")
    if not token:
        raise HTTPException(503, "API_TOKEN is not configured")
    if not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, "invalid token")


def run_pipeline(run_id):
    try:
        with connect() as conn:
            resume = conn.execute("SELECT id, text FROM resumes ORDER BY id DESC LIMIT 1").fetchone()
            postings, inactive_ids, _ = parse_listings(fetch_listings(), datetime.now(UTC))
            new = upsert_postings(conn, postings, inactive_ids)
            out = score_postings(
                conn, anthropic.Anthropic(), resume["id"], resume["text"], run_id=run_id
            )
            if out["failed_batches"] or out["unscored"]:
                log.warning("run %s: %s", run_id, out)
            dead = out["failed_batches"] and not out["scored"]
            conn.execute(
                "UPDATE runs SET status = %s, error = %s, finished_at = now(),"
                " postings_fetched = %s, postings_new = %s, postings_scored = %s,"
                " input_tokens = %s, output_tokens = %s WHERE id = %s",
                ("failed" if dead else "succeeded", "AllBatchesFailed" if dead else None,
                 len(postings), new, out["scored"], out["input_tokens"], out["output_tokens"],
                 run_id),
            )  # fmt: skip

    except Exception as e:
        log.exception("run %s failed", run_id)
        try:
            with connect() as conn:
                conn.execute(
                    "UPDATE runs SET status = 'failed', finished_at = now(), error = %s"
                    " WHERE id = %s",
                    (type(e).__name__, run_id),
                )
        except psycopg.Error:
            log.exception("run %s: could not record failure", run_id)


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


@app.post("/runs", status_code=202, dependencies=[Depends(auth)])
def start_run(tasks: BackgroundTasks):
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM resumes LIMIT 1").fetchone():
            raise HTTPException(409, "no resume")
        conn.execute(
            "UPDATE runs SET status = 'failed', finished_at = now(), error = 'stale'"
            " WHERE status = 'running' AND started_at < now() - interval '6 hours'"
        )
        try:
            run_id = conn.execute("INSERT INTO runs DEFAULT VALUES RETURNING id").fetchone()["id"]
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409, "a run is already running") from None
    tasks.add_task(run_pipeline, run_id)
    return {"run_id": run_id}


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
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Internship Scout</title>
<style>{CSS}</style>
</head>
<body>
<h1>Internship Scout</h1>
{stats}
<h2>Top matches</h2>
{matches}
<p><label>API token to explain a match <input id="token" type="password" autocomplete="off"></label></p>
<script>{JS}</script>
</body>
</html>"""
    return HTMLResponse(page, headers={"Content-Security-Policy": CSP})
