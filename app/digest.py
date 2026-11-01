import logging
import os
import tempfile
from datetime import date
from html import escape
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

MIN_SCORE = 70
MAX_MATCHES = 15
NOTE = "Scores are LLM estimates, so verify each posting before applying."

SELECT = """
SELECT m.id, m.score, m.summary, m.reasons, m.gaps, p.company, p.title, p.url, p.locations
FROM matches m JOIN postings p ON p.id = m.posting_id
WHERE m.resume_id = %s AND m.digested_at IS NULL AND m.score >= %s AND p.active
ORDER BY m.score DESC, m.id LIMIT %s
"""


def select_matches(conn, resume_id):
    return conn.execute(SELECT, (resume_id, MIN_SCORE, MAX_MATCHES)).fetchall()


def render(matches, scored_count):
    n = len(matches)
    subject = f"Internship Scout: {n} new match{'' if n == 1 else 'es'}"
    cards, lines = [], []
    for m in matches:
        title = escape(m["title"])
        if m["url"].startswith("https://"):
            title = f'<a href="{escape(m["url"])}" style="color:#0b57d0">{title}</a>'
        reasons = "".join(
            f'<li><b>{escape(r["requirement"])}</b>: {escape(r["evidence"])}</li>' for r in m["reasons"]
        )
        gaps = "".join(f"<li>{escape(g)}</li>" for g in m["gaps"])
        cards.append(
            '<div style="border:1px solid #ccc;border-radius:6px;padding:12px;margin:0 0 12px">'
            f'<div style="font-size:16px"><b>{m["score"]}</b> &middot; {escape(m["company"])}'
            f" &middot; {title}</div>"
            f'<p style="margin:8px 0">{escape(m["summary"])}</p>'
            f'<ul style="margin:4px 0;padding-left:20px">{reasons}</ul>'
            + (f'<div style="margin-top:8px"><b>Gaps</b></div><ul style="margin:4px 0;padding-left:20px">{gaps}</ul>' if gaps else "")
            + "</div>"
        )  # fmt: skip
        lines += [f"{m['score']} - {m['company']} - {m['title']}", m["url"], m["summary"]]
        lines += [f"  + {r['requirement']}: {r['evidence']}" for r in m["reasons"]]
        lines += [f"  - gap: {g}" for g in m["gaps"]] + [""]
    stats = f"{scored_count} postings scored this run. {NOTE}"
    html = (
        '<div style="font:14px/1.5 Arial,sans-serif;color:#222;max-width:640px;margin:0 auto">'
        f'<h2 style="margin:0 0 12px">{escape(subject)}</h2>{"".join(cards)}'
        f'<p style="color:#555;font-size:12px">{escape(stats)}</p></div>'
    )
    return subject, html, "\n".join([subject, "", *lines, stats])


def send(subject, html, text, client=None):
    r = (client or httpx).post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
        json={"from": os.environ["DIGEST_FROM"], "to": [os.environ["DIGEST_TO"]],
              "subject": subject, "html": html, "text": text},
        timeout=30,
    )  # fmt: skip
    r.raise_for_status()


def deliver(conn, resume_id, scored_count, client=None):
    matches = select_matches(conn, resume_id)
    if not matches:
        return False
    subject, html, text = render(matches, scored_count)
    env = os.environ
    if env.get("DIGEST_DRY_RUN") or not all(
        env.get(k) for k in ("RESEND_API_KEY", "DIGEST_FROM", "DIGEST_TO")
    ):
        path = Path(tempfile.gettempdir()) / f"internship-scout-digest-{date.today()}.html"
        path.write_text(html, encoding="utf-8")
        log.info("digest not sent, wrote %s", path)
        return False
    send(subject, html, text, client)
    # ponytail: a crash between send and mark could repeat an email once
    conn.execute(
        "UPDATE matches SET digested_at = now() WHERE id = ANY(%s)", ([m["id"] for m in matches],)
    )
    conn.commit()
    return True
