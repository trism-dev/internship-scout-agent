import json
from datetime import UTC, datetime, timedelta

import httpx
from psycopg.types.json import Jsonb

FEED_URL = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"
WINDOW_DAYS = 7

UPSERT = """
INSERT INTO postings
    (source, external_id, company, title, locations, url, terms, category, sponsorship,
     posted_at, active)
SELECT 'simplify', external_id, company, title, locations, url, terms, category, sponsorship,
       posted_at, true
FROM jsonb_to_recordset(%s) AS x(
    external_id text, company text, title text, locations jsonb, url text, terms jsonb,
    category text, sponsorship text, posted_at timestamptz)
ON CONFLICT (source, external_id) DO UPDATE SET
    title = EXCLUDED.title, company = EXCLUDED.company, locations = EXCLUDED.locations,
    url = EXCLUDED.url, terms = EXCLUDED.terms, category = EXCLUDED.category,
    sponsorship = EXCLUDED.sponsorship, posted_at = EXCLUDED.posted_at, active = true
RETURNING xmax = 0 AS inserted
"""


def fetch_listings(client=None):
    r = (client or httpx).get(FEED_URL, timeout=60)
    r.raise_for_status()
    return r.json()


def _check(x):
    text = json.dumps(x, ensure_ascii=False, allow_nan=False)
    text.encode()
    if "\u0000" in text:
        raise ValueError("NUL")


def parse_listings(raw, now):
    if not isinstance(raw, list):
        raise ValueError("feed is not a list")  # noqa: TRY004
    postings, inactive_ids, skipped = [], [], 0
    cutoff = now - timedelta(days=WINDOW_DAYS)
    for e in raw:
        try:
            if not isinstance(e["id"], str) or not e["id"]:
                raise ValueError
            if e["active"] is False or e.get("is_visible", True) is False:
                _check(e["id"])
                inactive_ids.append(e["id"])
                continue
            strs = [e["company_name"], e["title"], e["url"]]
            if (
                e["active"] is not True
                or not all(isinstance(s, str) and s for s in strs)
                or isinstance(e["date_posted"], bool)
                or not isinstance(e["date_posted"], (int, float))
            ):
                raise ValueError
            posted = datetime.fromtimestamp(e["date_posted"], UTC)
            posting = {
                "external_id": e["id"],
                "company": e["company_name"],
                "title": e["title"],
                "url": e["url"],
                "locations": e["locations"] if isinstance(e.get("locations"), list) else [],
                "terms": e["terms"] if isinstance(e.get("terms"), list) else [],
                "category": e["category"] if isinstance(e.get("category"), str) else None,
                "sponsorship": e["sponsorship"] if isinstance(e.get("sponsorship"), str) else None,
                "posted_at": posted.isoformat(),
            }
            _check(posting)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            skipped += 1
            continue
        if e["url"].startswith("https://") and posted >= cutoff:
            postings.append(posting)
    return postings, inactive_ids, skipped


def upsert_postings(conn, postings, inactive_ids):
    rows = list({p["external_id"]: p for p in postings}.values())
    new = 0
    if rows:
        new = sum(r["inserted"] for r in conn.execute(UPSERT, (Jsonb(rows),)))
    conn.execute(
        "UPDATE postings SET active = false WHERE source = 'simplify' AND external_id = ANY(%s)",
        (inactive_ids,),
    )
    conn.commit()
    return new
