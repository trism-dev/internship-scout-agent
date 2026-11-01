CREATE TABLE IF NOT EXISTS postings (
    id bigserial PRIMARY KEY,
    source text NOT NULL,
    external_id text NOT NULL,
    company text NOT NULL,
    title text NOT NULL,
    locations jsonb NOT NULL DEFAULT '[]',
    url text NOT NULL,
    terms jsonb NOT NULL DEFAULT '[]',
    category text,
    sponsorship text,
    posted_at timestamptz,
    active boolean NOT NULL DEFAULT true,
    description text,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS resumes (
    id bigserial PRIMARY KEY,
    text text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runs (
    id bigserial PRIMARY KEY,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'failed')),
    postings_fetched int NOT NULL DEFAULT 0,
    postings_new int NOT NULL DEFAULT 0,
    postings_scored int NOT NULL DEFAULT 0,
    input_tokens bigint NOT NULL DEFAULT 0,
    output_tokens bigint NOT NULL DEFAULT 0,
    digest_sent boolean NOT NULL DEFAULT false,
    error text
);

CREATE TABLE IF NOT EXISTS matches (
    id bigserial PRIMARY KEY,
    posting_id bigint NOT NULL REFERENCES postings,
    resume_id bigint NOT NULL REFERENCES resumes,
    run_id bigint REFERENCES runs,
    score int NOT NULL CHECK (score BETWEEN 0 AND 100),
    summary text NOT NULL,
    reasons jsonb NOT NULL DEFAULT '[]',
    gaps jsonb NOT NULL DEFAULT '[]',
    model text NOT NULL,
    scored_at timestamptz NOT NULL DEFAULT now(),
    digested_at timestamptz,
    UNIQUE (posting_id, resume_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_running_run ON runs (status) WHERE status = 'running';
