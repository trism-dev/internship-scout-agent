# internship-scout-agent

A weekly agent that pulls new internship postings from a public feed, scores each one against your resume using Claude with tool calling, explains every match, and emails a digest of the best ones.

Live: deploy pending.
<!-- Replace the line above with the real URL after the Railway deploy. -->

![Demo of the public page: explaining two matches](docs/demo.gif)

The GIF shows sample data at fictional companies (Example Robotics, Sample Labs and so on). It is not output from a real run. Re-record it against a deployed instance with `--live` (see Deploy).

## How it works

```mermaid
flowchart LR
    S[GitHub Actions weekly] -->|POST /runs| A[FastAPI app]
    F[Postings feed] -->|fetch and upsert| A
    A <--> D[(Postgres)]
    A -->|postings plus resume| C[Claude]
    C -->|get_posting_details, record_match| A
    A -->|top new matches| E[Digest email via Resend]
```

Each week GitHub Actions calls `POST /runs`. The app downloads the feed and stores new postings in Postgres. It then sends the unscored postings, 25 at a time, to Claude along with your resume. Claude has two tools: `get_posting_details` fetches a posting's description, and `record_match` saves a score from 0 to 100 with reasons and gaps. Matches scoring 70 or more that have not been emailed yet (at most 15) go into one HTML digest. Code sends the email, not the model.

The scorer's tests use a mocked API. No real scoring run has happened yet, so I make no claim about scoring quality, cost per run, or how many postings a week this finds.

## Design choices

- **Tool calling.** The feed has no descriptions. With tools, the model asks for a description only for postings that look promising, instead of one huge prompt with everything.
- **Cheapest model by default.** `SCOUT_MODEL` defaults to `claude-haiku-5-5`. Change the variable if the first real run scores poorly.
- **Manual loop.** The agent loop is a plain `while stop_reason == "tool_use"` over the Messages API, so there is no beta helper to break on an SDK upgrade.
- **Plain SQL.** psycopg and one `schema.sql` applied at startup. No ORM or migration tool until a second schema change needs one.
- **Posting text is untrusted.** The model can only name a posting by ID from this database, never a URL. Description fetches accept public IPs over https only, with redirect and size limits, and text is capped. Tools can only read a posting and save a score, and output is HTML-escaped. A hostile posting could still try to skew its own score; nothing here prevents that.
- **Explanations are behind a token.** The public page lists company, title and score only. An explanation quotes resume content, so it needs the bearer token.
- **No scheduler process.** Resend is called over HTTP and GitHub Actions is the scheduler, so the app is a single web service.

## Run locally

Needs Docker. Compose starts Postgres and the app on port 8000.

```bash
cp .env.example .env
# edit .env: set API_TOKEN to any long random string, and ANTHROPIC_API_KEY for a real run
docker compose up --build
```

In another shell (the app reads `.env`, so use the same token):

```bash
export TOKEN=your-api-token
curl localhost:8000/health

# upload a resume from a text file (jq builds the JSON body)
jq -Rs '{text: .}' resume.txt | curl -X PUT localhost:8000/resume \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d @-

# start a run (returns 202; it runs in the background and calls the real API)
curl -X POST localhost:8000/runs -H "Authorization: Bearer $TOKEN"

# check progress
curl localhost:8000/runs/latest

# list matches, then read one explanation
curl "localhost:8000/matches?min_score=70" -H "Authorization: Bearer $TOKEN"
curl localhost:8000/matches/1 -H "Authorization: Bearer $TOKEN"
```

Without `RESEND_API_KEY`, `DIGEST_FROM` and `DIGEST_TO`, or with `DIGEST_DRY_RUN` set, the digest is written to an HTML file in the system temp directory instead of being sent. `docker compose down -v` also removes the database volume.

## API

| Method and path | Auth | Purpose |
|---|---|---|
| `GET /health` | none | Database check |
| `GET /` | none | Public page: last run and top 10 matches |
| `GET /runs/latest` | none | Status and counts of the latest run |
| `PUT /resume` | bearer | Store a resume, `{"text": "..."}`, up to 50000 characters |
| `POST /runs` | bearer | Start a run in the background. 409 if no resume or a run is already running |
| `GET /matches` | bearer | List matches, query `min_score` (0 to 100) and `limit` (1 to 200) |
| `GET /matches/{id}` | bearer | Score, summary, reasons and gaps for one match |

## Tests

Tests need a Postgres database they may truncate. They are skipped if `TEST_DATABASE_URL` is unset.

```bash
pip install -r requirements-dev.txt
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres pytest --cov=app
ruff check .
```

CI runs the same against a Postgres service.

## Environment variables

| Variable | Meaning |
|---|---|
| `DATABASE_URL` | Postgres connection string (compose overrides it for the app container) |
| `API_TOKEN` | Bearer token for protected endpoints. If empty, they return 503 |
| `ANTHROPIC_API_KEY` | Used by the Anthropic SDK for scoring |
| `SCOUT_MODEL` | Model for scoring, default `claude-haiku-5-5` |
| `SCOUT_MAX_POSTINGS` | Maximum postings scored per run, default 1000 |
| `RESEND_API_KEY` | Resend key for sending the digest |
| `DIGEST_FROM` | Sender address |
| `DIGEST_TO` | Recipient address |
| `DIGEST_DRY_RUN` | If set, write the digest to a file instead of sending |

## Deploy

Not done yet. Steps for Railway:

1. Create a Railway project from this repo and add a Postgres service. `railway.toml` selects the Dockerfile build and the `/health` check.
2. On the app service set `DATABASE_URL` (reference the Postgres service), `API_TOKEN`, `ANTHROPIC_API_KEY`, `RESEND_API_KEY`, `DIGEST_FROM` and `DIGEST_TO`. Generate a public domain.
3. Upload your resume with `PUT /resume`. Never commit it; `resume*` files are gitignored.
4. In the GitHub repo add the secret `API_TOKEN` (same value) and the variable `SCOUT_URL` (the public base URL, no trailing slash). `weekly.yml` then calls `POST /runs` every Monday at 13:00 UTC and can be run by hand with workflow dispatch. It does nothing while `SCOUT_URL` is unset.
5. Replace "Live: deploy pending." above with the URL.
6. Optionally re-record the GIF from the real site: `API_TOKEN=... python scripts/record_demo.py --live --url https://your-url`. This needs `pip install playwright pillow` and an installed Edge or Chrome (`--channel chrome`). Without `--live` it seeds fictional sample data into the database at `DATABASE_URL` and captions the frames "Sample data".

## Data source

Postings come from the [SimplifyJobs Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships) `listings.json`, read only. Each match links back to the original posting. This repo declares no license of its own.
