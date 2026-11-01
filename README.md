# internship-scout-agent

A weekly agent that pulls new internship postings from a public feed, scores each one against your resume using Claude with tool calling, explains every match, and emails a digest of the best ones.

Live: https://internship-scout-agent.vercel.app (my own instance). Full example with sample data: [/demo](https://internship-scout-agent.vercel.app/demo).

Each instance serves one person: one resume, one set of keys, one digest. To use it yourself, run your own copy on free tiers with your own keys (see [Run your own copy](#run-your-own-copy)). [Scaling to multiple users](#scaling-to-multiple-users) explains what a shared, multi-user version would need and why this one stays single-user.

![Demo of the public page: explaining two matches](docs/demo.gif)

The GIF shows sample data at fictional companies (Example Robotics, Sample Labs and so on). It is not output from a real run. Re-record it against a deployed instance with `--live` (see Run your own copy).

## How it works

```mermaid
flowchart LR
    S[GitHub Actions weekly] -->|python -m app.run| J[Scoring job]
    F[Postings feed] -->|fetch and upsert| J
    J <--> D[(Postgres)]
    J -->|postings plus resume| C[Claude]
    C -->|get_posting_details, record_match| J
    J -->|top new matches| E[Digest email via Resend]
    V[Vercel: API and page] <--> D
```

Each week GitHub Actions runs `python -m app.run`. The job downloads the feed and stores new postings in Postgres. It then sends the unscored postings, 25 at a time, to Claude along with your resume. Claude has two tools: `get_posting_details` fetches a posting's description, and `record_match` saves a score from 0 to 100 with reasons and gaps. Matches scoring 70 or more that have not been emailed yet (at most 15) go into one HTML digest. Code sends the email, not the model. The FastAPI app on Vercel only reads the same database to serve the API and the public page, and takes the resume upload.

The scorer's tests use a mocked API. The first real run (1 Nov 2026, `claude-haiku-5-5`) scored all 585 active postings in the feed in 16 minutes, using 722,652 input and 240,066 output tokens, and sent the digest. That first run scores the whole feed; later weekly runs score only new postings. Some career sites block description fetches (HTTP 403), so those postings are scored from title, company and location alone. Scores are the model's judgment, not checked against a labelled set, so I make no accuracy claim.

## Design choices

- **Tool calling.** The feed has no descriptions. With tools, the model asks for a description only for postings that look promising, instead of one huge prompt with everything.
- **Cheapest model by default.** `SCOUT_MODEL` defaults to `claude-haiku-5-5`. The first real run's top matches and explanations held up on a read-through, so it stays the default; change the variable if yours score poorly.
- **Manual loop.** The agent loop is a plain loop over the Messages API with a turn limit, so there is no beta helper to break on an SDK upgrade.
- **Plain SQL.** psycopg and one `schema.sql` applied at startup. No ORM or migration tool until a second schema change needs one.
- **Posting text is untrusted.** The model can only name a posting by ID from this database, never a URL. Description fetches accept public IPs over https only, with redirect and size limits, and text is capped. Tools can only read a posting and save a score, and output is HTML-escaped. A hostile posting could still try to skew its own score; nothing here prevents that.
- **Explanations are behind a token.** The public page lists company, title and score only. An explanation quotes resume content, so it needs the bearer token.
- **Scoring runs in Actions, not in the web app.** A run takes longer than the 300 second function limit on Vercel's Hobby plan ([limits](https://vercel.com/docs/functions/limitations)), so GitHub Actions runs it as a job (up to 180 minutes) and the web app only serves the API and page. Resend is called over HTTP, so there is no other process to run.

## Run locally

Needs Docker. Compose starts Postgres and the app on port 8000. The commands below are for bash (Git Bash or WSL on Windows).

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
```

Start a run in the app container (it calls the real API and exits non-zero unless the run succeeds):

```bash
docker compose exec app python -m app.run
```

Then check the result and read matches:

```bash
# check the latest run
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
| `GET /demo` | none | Sample resume and matches with full explanations, fictional data, no database |
| `GET /runs/latest` | none | Status and counts of the latest run |
| `PUT /resume` | bearer | Store a resume, `{"text": "..."}`, up to 50000 characters |
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

| Variable | Meaning | Set it in |
|---|---|---|
| `DATABASE_URL` | Postgres connection string. On Vercel use the pooled Neon string, in GitHub the direct one (compose overrides it for the app container) | Vercel env, GitHub secret, `.env` |
| `API_TOKEN` | Bearer token for protected endpoints. If empty, they return 503 | Vercel env, `.env` |
| `ANTHROPIC_API_KEY` | Used by the Anthropic SDK for scoring | GitHub secret, `.env` |
| `SCOUT_MODEL` | Model for scoring, default `claude-haiku-5-5` (empty means the default) | GitHub variable, `.env` |
| `SCOUT_MAX_POSTINGS` | Maximum postings scored per run, default 1000 | `.env` |
| `RESEND_API_KEY` | Resend key for sending the digest | GitHub secret, `.env` |
| `DIGEST_FROM` | Sender address | GitHub secret, `.env` |
| `DIGEST_TO` | Recipient address | GitHub secret, `.env` |
| `DIGEST_DRY_RUN` | If set, write the digest to a file instead of sending | `.env` |
| `SCOUT_ENABLED` | Set to `true` to let `weekly.yml` run | GitHub variable |

## Run your own copy

Your copy runs on Neon (Postgres), Vercel (API and page) and GitHub Actions (the weekly scoring job), all on free plans. You pay only for Claude API usage. You need accounts at GitHub, Neon, Vercel, Anthropic (with API credit) and, for the email digest, Resend. If a coding agent is doing the setup for you, give it this section: steps that need your accounts or keys are yours to do by hand, and keys never go in a file in the repo.

1. Fork this repo. GitHub turns Actions off on a fork until you open its Actions tab and enable workflows.
2. Create a Neon project on the free plan. Copy both connection strings: the pooled one (the host contains `-pooler`) and the direct one.
3. Import your fork in Vercel on the Hobby plan. Hobby is for personal, non-commercial use ([terms](https://vercel.com/docs/plans/hobby)). Set the Framework Preset to FastAPI. Vercel finds `app/main.py` and installs the dependencies from `pyproject.toml`, not `requirements.txt`; a test keeps the two lists equal. Set `DATABASE_URL` to the pooled string and `API_TOKEN` to a long random string. The scoring keys are not needed on Vercel. After the deploy, `https://your-url/health` should return `{"status":"ok"}`.
4. Upload your resume as plain text with `PUT /resume` (the `jq` and `curl` command under Run locally, with your Vercel URL in place of `localhost:8000`). Never commit it; `resume*` files are gitignored.
5. In your fork add the secrets `DATABASE_URL` (the direct string), `ANTHROPIC_API_KEY`, `RESEND_API_KEY`, `DIGEST_FROM` and `DIGEST_TO`, and the variable `SCOUT_ENABLED` set to `true`. `weekly.yml` then runs every Monday at 13:00 UTC. It does nothing while `SCOUT_ENABLED` is unset. Start the first run by hand with `gh workflow run weekly.yml`. A job that is cancelled or times out leaves its run marked as running, which blocks new runs until the 4 hour stale check clears it. The email secrets are optional: without all three, the digest is skipped and matches are only on the site and API. Resend's test sender `onboarding@resend.dev` delivers only to your own Resend account address; to send elsewhere, verify a domain in Resend.
6. Read the results with `GET /runs/latest` and `GET /matches` (see API).
7. Optionally re-record the GIF from the real site: `python scripts/record_demo.py --live --url https://your-url`. It records only the public table, with no token and no Explain click, because explanations quote your resume. This needs `pip install playwright pillow` and an installed Edge or Chrome (`--channel chrome`). Without `--live` it seeds fictional sample data into a local database at `DATABASE_URL` (it refuses any other host or a database with real data) and captions the frames "Sample data".

## Scaling to multiple users

This version is single-user on purpose: every user's weekly run is real Claude API spend, and a personal project should not carry other people's costs. A shared version would need:

- **Sign-in.** Email magic links sent through Resend from a verified domain, with a `users` table and a session cookie in place of the single bearer token.
- **Per-user data.** A `user_id` on resumes and runs. Matches are already keyed by resume, so scoring and the digest need no new logic, only a loop over users in the weekly job.
- **Cost control.** A free keyword prefilter so Claude scores only each user's most likely postings, a per-user weekly cap, and a global token budget that stops runs when reached. `runs` already records token counts to enforce it.
- **Abuse limits.** Rate limits on sign-in and resume upload, and a cap on users with a waitlist past it.
- **Privacy.** Resumes are personal data: a delete-my-account endpoint that removes the resume and its matches, and a short privacy note.
- **Hosting.** Vercel's Hobby plan is for non-commercial use only, so a shared service open to the public may need a paid plan.

## Data source

Postings come from the [SimplifyJobs Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships) `listings.json`, read only. Each match links back to the original posting. The feed has its own terms; this repo's MIT license (see `LICENSE`) covers only the code here.
