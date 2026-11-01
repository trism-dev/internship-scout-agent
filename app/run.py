"""Weekly scoring run: `python -m app.run`. Scheduled by GitHub Actions (see weekly.yml)."""

import logging
import traceback
from contextlib import suppress
from datetime import UTC, datetime

import anthropic
import psycopg

from app.agent import score_postings
from app.db import apply_schema, connect
from app.digest import deliver
from app.feed import fetch_listings, parse_listings, upsert_postings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


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
            conn.commit()
            if not dead:
                try:
                    if deliver(conn, resume["id"], out["scored"]):
                        conn.execute("UPDATE runs SET digest_sent = true WHERE id = %s", (run_id,))
                except Exception as e:  # noqa: BLE001 - nothing from the digest may fail a succeeded run
                    with suppress(psycopg.Error):
                        conn.rollback()
                    log.error("run %s: digest failed: %s", run_id, type(e).__name__)
    except Exception as e:  # noqa: BLE001 - any failure is recorded on the run row
        # Actions logs may be public: log the type and frames, never the message (it can quote text).
        frames = "".join(traceback.format_tb(e.__traceback__))
        log.error("run %s failed: %s\n%s", run_id, type(e).__name__, frames)
        try:
            with connect() as conn:
                conn.execute(
                    "UPDATE runs SET status = 'failed', finished_at = now(), error = %s"
                    " WHERE id = %s",
                    (type(e).__name__, run_id),
                )
        except psycopg.Error as e2:
            log.error("run %s: could not record failure: %s", run_id, type(e2).__name__)


def main():
    with connect() as conn:
        apply_schema(conn)
        if not conn.execute("SELECT 1 FROM resumes LIMIT 1").fetchone():
            raise SystemExit("no resume")
        conn.execute(
            "UPDATE runs SET status = 'failed', finished_at = now(), error = 'stale'"
            " WHERE status = 'running' AND started_at < now() - interval '6 hours'"
        )
        try:
            run_id = conn.execute("INSERT INTO runs DEFAULT VALUES RETURNING id").fetchone()["id"]
        except psycopg.errors.UniqueViolation:
            raise SystemExit("a run is already running") from None
        conn.commit()
    run_pipeline(run_id)
    with connect() as conn:
        run = conn.execute(
            "SELECT status, postings_fetched, postings_new, postings_scored, input_tokens,"
            " output_tokens, digest_sent FROM runs WHERE id = %s",
            (run_id,),
        ).fetchone()
    print(f"run {run_id}: " + ", ".join(f"{k}={v}" for k, v in run.items()))
    if run["status"] != "succeeded":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
