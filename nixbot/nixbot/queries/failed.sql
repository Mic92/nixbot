-- Failed-build cache and failed-status records (failed_builds.py,
-- status.py, recovery.py retention).

-- name: FailedBuildByDrv :one
SELECT derivation, timestamp, url FROM failed_builds
WHERE project_id = $1 AND derivation = $2;

-- name: UpsertFailedBuild :exec
INSERT INTO failed_builds (project_id, derivation, timestamp, url)
VALUES ($1, $2, $3, $4)
ON CONFLICT (project_id, derivation)
DO UPDATE SET timestamp = EXCLUDED.timestamp, url = EXCLUDED.url;

-- name: UpsertFailedStatus :exec
INSERT INTO failed_statuses (revision, status_name, timestamp)
VALUES ($1, $2, $3)
ON CONFLICT (revision, status_name)
DO UPDATE SET timestamp = EXCLUDED.timestamp;

-- name: FailedStatusNames :many
SELECT status_name FROM failed_statuses WHERE revision = $1;

-- name: ClearFailedStatus :exec
DELETE FROM failed_statuses WHERE revision = $1 AND status_name = $2;

-- name: PruneOldFailedStatuses :exec
DELETE FROM failed_statuses
WHERE to_timestamp(timestamp) < now() - make_interval(days => sqlc.arg(retention_days)::int);

-- name: PruneOldFailedBuilds :exec
DELETE FROM failed_builds
WHERE to_timestamp(timestamp) < now() - make_interval(days => sqlc.arg(retention_days)::int);

-- name: GetCheckRunId :one
SELECT external_id FROM check_runs
WHERE project_id = $1 AND sha = $2 AND name = $3;

-- name: CheckRunAttr :one
-- Unwrap ('', attr): :one returns NULL for both "no row" and a NULL
-- attr, but the rerequested handler must distinguish a summary run
-- (row with NULL attr -> full restart) from an unknown name (no row).
SELECT '' AS found, attr FROM check_runs
WHERE project_id = $1 AND sha = $2 AND name = $3;

-- name: UpsertCheckRun :exec
INSERT INTO check_runs (project_id, sha, name, attr, external_id, timestamp)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (project_id, sha, name)
DO UPDATE SET attr = EXCLUDED.attr, external_id = EXCLUDED.external_id,
              timestamp = EXCLUDED.timestamp;

-- name: LatestBuildForSha :one
SELECT id FROM builds WHERE project_id = $1 AND commit_sha = $2
ORDER BY id DESC LIMIT 1;
