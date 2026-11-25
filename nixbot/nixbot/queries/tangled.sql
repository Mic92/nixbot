-- Tangled builds created by tack (tangled.py).

-- name: TangledPullNumber :one
SELECT number FROM tangled_pulls
WHERE project_id = $1 AND source_branch = $2 AND target_branch = $3;

-- name: AllocateTangledPull :one
-- Callers hold the project's advisory lock (LockBuildIdentity).
INSERT INTO tangled_pulls (project_id, source_branch, target_branch, number)
SELECT sqlc.arg(project_id)::bigint, sqlc.arg(source_branch)::text,
       sqlc.arg(target_branch)::text, COALESCE(max(number), 0) + 1
FROM tangled_pulls WHERE project_id = sqlc.arg(project_id)::bigint
RETURNING number;

-- name: CreateTangledBuild :one
INSERT INTO tangled_builds
    (project_id, commit_sha, branch, base_branch, tag, pr_number, message,
     meta_data, build_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
RETURNING *;

-- name: TangledBuildByNumber :one
SELECT * FROM tangled_builds WHERE project_id = $1 AND number = $2;

-- name: LastLinkedTangledBuild :one
-- The previous build tack requested for the same commit and context.
SELECT * FROM tangled_builds
WHERE project_id = sqlc.arg(project_id)
  AND (tag IS NOT NULL OR commit_sha = sqlc.arg(commit_sha))
  AND COALESCE(tag, base_branch, branch) = sqlc.arg(branch)::text
  AND tag IS NOT DISTINCT FROM sqlc.narg(tag)::text
  AND pr_number IS NOT DISTINCT FROM sqlc.narg(pr_number)::bigint
  AND build_id IS NOT NULL
ORDER BY number DESC LIMIT 1;

-- name: AdvanceTangledBuilds :exec
-- A nixbot build reported for an event: move every tack build waiting
-- on that event, and every one already attached to the nixbot build.
-- The latter covers builds shared across contexts, whose later reports
-- carry the identity of whichever context created them.
UPDATE tangled_builds SET
    state = sqlc.arg(state),
    build_id = sqlc.arg(build_id),
    started_at = COALESCE(started_at, now()),
    finished_at = CASE WHEN sqlc.arg(state) IN ('scheduled', 'running')
                       THEN NULL ELSE now() END
WHERE project_id = sqlc.arg(project_id)
  AND (build_id = sqlc.arg(build_id)
       OR (state IN ('scheduled', 'running')
           AND (tag IS NOT NULL OR commit_sha = sqlc.arg(commit_sha))
           AND COALESCE(tag, base_branch, branch) = sqlc.arg(branch)::text
           AND tag IS NOT DISTINCT FROM sqlc.narg(tag)::text
           AND pr_number IS NOT DISTINCT FROM sqlc.narg(pr_number)::bigint));

-- name: SettleUnbuiltTangledBuilds :exec
-- The change was processed without a nixbot build reporting for it.
UPDATE tangled_builds SET
    state = sqlc.arg(state), error = sqlc.narg(error), finished_at = now()
WHERE project_id = sqlc.arg(project_id)
  AND state = 'scheduled'
  AND build_id IS NULL
  AND (tag IS NOT NULL OR commit_sha = sqlc.arg(commit_sha))
  AND COALESCE(tag, base_branch, branch) = sqlc.arg(branch)::text
  AND tag IS NOT DISTINCT FROM sqlc.narg(tag)::text
  AND pr_number IS NOT DISTINCT FROM sqlc.narg(pr_number)::bigint;

-- name: PendingTangledNotifications :many
SELECT * FROM tangled_builds WHERE notified_state <> state
ORDER BY number LIMIT 100;

-- name: MarkTangledNotified :exec
UPDATE tangled_builds SET notified_state = $2 WHERE id = $1;

-- name: TangledBuildAttributes :many
SELECT attr, status, cached FROM build_attributes
WHERE build_id = $1 ORDER BY attr;

-- name: CleanupTangledBuilds :exec
DELETE FROM tangled_builds
WHERE created_at < now() - make_interval(days => sqlc.arg(days)::int);
