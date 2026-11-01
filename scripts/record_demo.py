"""Record docs/demo.gif of the public page.

Needs Playwright and Pillow. They are installed only for this script and are not in
requirements.txt or requirements-dev.txt:  pip install playwright pillow
No browser download: it uses an installed Edge or Chrome (--channel).

Default (sample mode): seeds a fictional resume and 6 matches at fictional companies into
DATABASE_URL, expects a server already running at --url against that same database, and
stamps "Sample data" on every frame. Use a throwaway database, not a real one: it refuses to
run if the database holds postings from the real feed.
--live: seeds nothing, records the real site, no caption.

The bearer token is read from the API_TOKEN environment variable, never from argv.
"""

import argparse
import io
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESUME = (
    "Sample Student (fictional). BS Computer Science, Example University, 2027. "
    "Python, SQL, FastAPI. Built a warehouse routing simulator. Intern project: robot arm "
    "telemetry dashboard. Interests: robotics, data pipelines."
)

# company, title, score, summary, reasons [(requirement, evidence)], gaps
SAMPLES = [
    (
        "Example Robotics",
        "Software Engineering Intern",
        92,
        "Robotics software role that matches the telemetry and Python experience.",
        [
            ("Python and data pipelines", "Telemetry dashboard project in Python"),
            ("Interest in robotics", "Warehouse routing simulator"),
        ],
        ["No ROS experience listed"],
    ),
    (
        "Sample Labs",
        "Data Engineering Intern",
        85,
        "Pipeline work that fits the SQL and FastAPI background.",
        [("SQL", "SQL listed in skills"), ("Backend services", "FastAPI listed in skills")],
        ["No cloud warehouse experience"],
    ),
    (
        "Placeholder Systems",
        "Backend Intern",
        78,
        "Backend role with a reasonable overlap.",
        [("Python backend", "FastAPI listed in skills")],
        ["No production deployment shown"],
    ),
    (
        "Demo Analytics",
        "Analytics Intern",
        66,
        "Some overlap in SQL; the role is more statistics than software.",
        [("SQL", "SQL listed in skills")],
        ["No statistics coursework listed"],
    ),
    (
        "Fictional Dynamics",
        "Embedded Software Intern",
        54,
        "Embedded focus; the resume shows little low-level work.",
        [("Interest in hardware", "Robot arm telemetry project")],
        ["No C or C++ listed"],
    ),
    (
        "Acme Sample Corp",
        "IT Support Intern",
        31,
        "Mostly support work, a weak fit.",
        [],
        ["No support experience listed"],
    ),
]


def seed():
    from psycopg.types.json import Jsonb

    from app.db import apply_schema, connect

    with connect() as conn:
        apply_schema(conn)
        real = conn.execute(
            "SELECT count(*) AS n FROM postings WHERE source <> 'sample'"
        ).fetchone()
        if real["n"]:
            sys.exit("Refusing to seed: this database has postings from the real feed.")
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
    ap.add_argument("--live", action="store_true", help="record the real site, seed nothing")
    ap.add_argument("--channel", default="msedge", help="msedge or chrome")
    ap.add_argument("--out", default="docs/demo.gif")
    args = ap.parse_args()
    token = os.environ.get("API_TOKEN")
    if not token:
        sys.exit("Set API_TOKEN in the environment.")
    if not args.live:
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
        page.wait_for_selector("button[data-id]")
        snap(page, 1500)
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
