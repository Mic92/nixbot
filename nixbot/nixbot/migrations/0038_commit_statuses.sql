-- The last status nixbot posted per (project, sha, context), on any
-- forge. Replaces check_runs (GitHub run ids) and failed_statuses
-- (failed contexts), so the gate and the builds share one record.
CREATE TABLE commit_statuses (
    project_id  BIGINT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    sha         TEXT   NOT NULL,
    context     TEXT   NOT NULL,
    state       TEXT   NOT NULL,
    -- Per-attribute contexts, so a GitHub re-run restarts only that.
    attr        TEXT,
    -- GitHub check-run id, NULL on other forges.
    run_id      BIGINT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, sha, context)
);

-- check_runs never recorded a state. Only failures matter for later
-- posts, so everything else counts as success.
INSERT INTO commit_statuses (project_id, sha, context, state, attr, run_id, updated_at)
SELECT c.project_id, c.sha, c.name,
       CASE WHEN EXISTS (
           SELECT 1 FROM failed_statuses f
           WHERE f.revision = c.sha AND f.status_name = c.name
       ) THEN 'failure' ELSE 'success' END,
       c.attr, c.external_id, to_timestamp(c.timestamp)
FROM check_runs c;

-- Gitea and GitLab failures have no check run. failed_statuses lacks
-- the project, so take it from the builds of that commit.
INSERT INTO commit_statuses (project_id, sha, context, state, updated_at)
SELECT DISTINCT b.project_id, f.revision, f.status_name, 'failure',
       to_timestamp(f.timestamp)
FROM failed_statuses f
JOIN builds b ON b.commit_sha = f.revision
ON CONFLICT DO NOTHING;

DROP TABLE check_runs;
DROP TABLE failed_statuses;
