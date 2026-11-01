# internship-scout-agent
Weekly agent: pulls new internship postings, scores them against a resume with Claude tool calling, emails a digest.
- Layout: `app/` (main, db, feed, agent, digest, schema.sql), `tests/`, `scripts/record_demo.py`, `docs/demo.gif`.
- Plain SQL with psycopg, no ORM. Posting text is untrusted: the model's tools only read a posting by ID and save a score.
- Checks: `ruff check .` and `TEST_DATABASE_URL=postgresql://... pytest --cov=app --cov-fail-under=80`.
- Tests never call the real API (mock transport via httpx2 for the anthropic SDK).
- Real resumes are never committed (`resume*` is gitignored). Demo and tests use a fictional sample.
- `scripts/record_demo.py` needs playwright and pillow, installed by hand, not in requirements.
- Push only feature branches; deploy, PRs and merges need the owner's approval.
