-- Tangled support. Tack (a Tangled spindle) drives nixbot through a
-- Buildkite-compatible API, see nixbot/tangled.py.

-- Tack reports a pull request only as source and target branch, so the
-- number that makes it a PR build here is allocated locally.
CREATE TABLE tangled_pulls (
    project_id    BIGINT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    source_branch TEXT   NOT NULL,
    target_branch TEXT   NOT NULL,
    number        BIGINT NOT NULL,
    PRIMARY KEY (project_id, source_branch, target_branch),
    UNIQUE (project_id, number)
);

-- One row per build tack created. It exists before the nixbot build:
-- the API has to answer with an id and a number right away, while the
-- change is only queued.
CREATE TABLE tangled_builds (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    number         BIGINT NOT NULL GENERATED ALWAYS AS IDENTITY UNIQUE,
    project_id     BIGINT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    commit_sha     TEXT   NOT NULL,
    -- As tack sent it: the source branch for pull requests, the full
    -- ref for tags.
    branch         TEXT   NOT NULL,
    base_branch    TEXT,
    -- Tag name of a tag push. Its build reports for the commit the tag
    -- peels to, which can differ from commit_sha.
    tag            TEXT,
    pr_number      BIGINT,
    message        TEXT   NOT NULL DEFAULT '',
    -- Returned verbatim. Tack finds its pipeline through it.
    meta_data      JSONB  NOT NULL DEFAULT '{}',
    -- Buildkite build state: scheduled | running | passed | failed | canceled
    state          TEXT   NOT NULL DEFAULT 'scheduled',
    -- Last state tack acknowledged. Differs from state while a webhook
    -- is outstanding.
    notified_state TEXT   NOT NULL DEFAULT 'scheduled',
    -- Why no nixbot build exists (e.g. the commit could not be fetched).
    error          TEXT,
    build_id       BIGINT REFERENCES builds (id) ON DELETE SET NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at     TIMESTAMPTZ,
    finished_at    TIMESTAMPTZ
);

CREATE INDEX tangled_builds_event_idx ON tangled_builds (project_id, commit_sha);
CREATE INDEX tangled_builds_build_idx ON tangled_builds (build_id);
CREATE INDEX tangled_builds_pending_idx ON tangled_builds (number)
    WHERE notified_state <> state;
