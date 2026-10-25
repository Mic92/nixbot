-- The last status posted per commit and context (status.py).

-- name: GetCommitStatus :one
SELECT state, attr, run_id FROM commit_statuses
WHERE project_id = $1 AND sha = $2 AND context = $3;

-- name: UpsertCommitStatus :exec
INSERT INTO commit_statuses (project_id, sha, context, state, attr, run_id)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (project_id, sha, context)
DO UPDATE SET state = EXCLUDED.state, attr = EXCLUDED.attr,
              run_id = EXCLUDED.run_id, updated_at = now();

-- name: FailedCommitStatuses :many
SELECT context, attr FROM commit_statuses
WHERE project_id = $1 AND sha = $2 AND state = 'failure';

-- name: LatestBuildForSha :one
SELECT id FROM builds WHERE project_id = $1 AND commit_sha = $2
ORDER BY id DESC LIMIT 1;
