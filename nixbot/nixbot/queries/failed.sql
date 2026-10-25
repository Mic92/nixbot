-- Failed-build cache (failed_builds.py, recovery.py retention).

-- name: FailedBuildByDrv :one
SELECT derivation, timestamp, url FROM failed_builds
WHERE project_id = $1 AND derivation = $2;

-- name: UpsertFailedBuild :exec
INSERT INTO failed_builds (project_id, derivation, timestamp, url)
VALUES ($1, $2, $3, $4)
ON CONFLICT (project_id, derivation)
DO UPDATE SET timestamp = EXCLUDED.timestamp, url = EXCLUDED.url;
