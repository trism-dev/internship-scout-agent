"""Record docs/demo.gif of the public page.

Needs Playwright and Pillow. They are installed only for this script and are not in
requirements.txt or requirements-dev.txt:  pip install playwright pillow
No browser download: it uses an installed Edge or Chrome (--channel).

Default (sample mode): seeds a fictional resume and 6 matches at fictional companies into
DATABASE_URL, expects a server already running at --url against that same database, and
stamps "Sample data" on every frame. It refuses unless the database host is localhost or
127.0.0.1, and unless resumes, runs and postings hold nothing but an earlier run of this
script (those rows are deleted first, so it can be re-run). It prints the database name.
--live: seeds nothing and records only the public table of the real site. No token and no
Explain click, because explanations quote the resume.

Sample mode reads the bearer token from the API_TOKEN environment variable, never from argv.
"""

import argparse
import io
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.demo_data import RESUME, SAMPLES


def seed():
    from urllib.parse import urlparse

    from psycopg.types.json import Jsonb

    from app.db import apply_schema, connect

    url = urlparse(os.environ["DATABASE_URL"])
    if url.hostname not in ("localhost", "127.0.0.1"):
        sys.exit(f"Refusing to seed: database host {url.hostname!r} is not localhost or 127.0.0.1.")
    print(f"Seeding database {url.path.lstrip('/')!r} on {url.hostname}")
    with connect() as conn:
        apply_schema(conn)
        # Remove an earlier run of this script, then require the database to be empty.
        # Leaving via sys.exit inside this block rolls the deletes back.
        conn.execute(
            "CREATE TEMP TABLE old_runs AS SELECT DISTINCT m.run_id FROM matches m"
            " JOIN postings p ON p.id = m.posting_id WHERE p.source = 'sample'"
            " AND m.run_id IS NOT NULL"
        )
        conn.execute(
            "DELETE FROM matches WHERE posting_id IN (SELECT id FROM postings WHERE source = 'sample')"
        )
        conn.execute("DELETE FROM runs WHERE id IN (SELECT run_id FROM old_runs)")
        conn.execute("DELETE FROM postings WHERE source = 'sample'")
        conn.execute("DELETE FROM resumes WHERE text = %s", (RESUME,))
        for table in ("resumes", "runs", "postings", "matches"):
            if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                sys.exit(f"Refusing to seed: {table} holds data that is not sample data.")
        rid = conn.execute(
            "INSERT INTO resumes (text) VALUES (%s) RETURNING id", (RESUME,)
        ).fetchone()["id"]
        run = conn.execute(
            "INSERT INTO runs (status, finished_at, postings_scored) VALUES ('succeeded', now(), %s)"
            " RETURNING id", (len(SAMPLES),),
        ).fetchone()["id"]  # fmt: skip
        for i, (company, title, score, summary, reasons, gaps) in enumerate(SAMPLES):
            pid = conn.execute(
                "INSERT INTO postings (source, external_id, company, title, url)"
                " VALUES ('sample', %s, %s, %s, %s) RETURNING id",
                (f"sample-{i}", company, title, f"https://example.com/jobs/{i}"),
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO matches (posting_id, resume_id, run_id, score, summary, reasons, gaps, model)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, 'sample')",
                (pid, rid, run, score, summary,
                 Jsonb([{"requirement": a, "evidence": b} for a, b in reasons]), Jsonb(gaps)),
            )  # fmt: skip


def font(size):
    for name in ("C:/Windows/Fonts/arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def caption(png, text):
    img = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = img.size
    out = Image.new("RGB", (w, h + 34), "#b45309")
    out.paste(img, (0, 0))
    ImageDraw.Draw(out).text((12, h + 5), text, fill="white", font=font(20))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument(
        "--live",
        action="store_true",
        help="record only the public table of the real site, seed nothing",
    )
    ap.add_argument("--channel", default="msedge", help="msedge or chrome")
    ap.add_argument("--out", default="docs/demo.gif")
    args = ap.parse_args()
    token = os.environ.get("API_TOKEN")
    if not args.live:
        if not token:
            sys.exit("Set API_TOKEN in the environment.")
        seed()

    frames, console = [], []

    def snap(page, ms):
        png = page.screenshot()
        frames.append((png if args.live else caption(png, "Sample data"), ms))

    with sync_playwright() as p:
        browser = p.chromium.launch(channel=args.channel)
        page = browser.new_page(viewport={"width": 860, "height": 680})
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}"))
        page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))
        page.goto(args.url)
        page.wait_for_selector("h2")
        snap(page, 3000 if args.live else 1500)
        if not args.live:  # explanations quote the resume, so never open them on a real site
            page.type("#token", token, delay=80)
            snap(page, 800)
            buttons = page.locator("button[data-id]")
            for i in (0, 1):
                buttons.nth(i).click()
                page.wait_for_selector(f"tr:nth-child({i + 1}) .x ul", timeout=10000)
                snap(page, 2800)
        browser.close()

    imgs = [
        f if isinstance(f, Image.Image) else Image.open(io.BytesIO(f)).convert("RGB")
        for f, _ in frames
    ]
    imgs = [i.quantize(colors=64, method=Image.Quantize.MEDIANCUT) for i in imgs]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    imgs[0].save(args.out, save_all=True, append_images=imgs[1:], duration=[ms for _, ms in frames],
                 loop=0, optimize=True)  # fmt: skip
    print(f"wrote {args.out}, {Path(args.out).stat().st_size} bytes, {len(imgs)} frames")
    print("console messages:", console or "none")


if __name__ == "__main__":
    main()
