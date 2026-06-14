-- Restart, rerun, recovery, cancellation and retention queries
-- (restarts.py, reruns.py, recovery.py, service.py, effects_run.py,
-- build_reuse.py, reconcile.py).

-- name: AttributeKnown :one
SELECT 1 AS one FROM build_attributes WHERE build_id = $1 AND attr = $2;

-- name: ResetBuildForRestart :exec
-- One atomic statement for a restart (attr NULL = full restart):
-- clear cached failures so the attributes actually build again
-- instead of re-skipping, reset the targeted attribute rows, and
-- requeue the build. A full restart also re-runs effects; a partial
-- rebuild must not re-deploy. The build ends up queued, not started:
-- the rerun decides whether this becomes a re-eval (evaluating) or
-- an attribute rerun (building). Clearing finished_at keeps
-- retention cleanup off a build that is about to rerun; clearing
-- error/eval_warnings keeps a stale failure banner off a restart
-- that succeeds.
WITH cleared_failures AS (
    DELETE FROM failed_builds WHERE project_id =
        (SELECT project_id FROM builds
         WHERE builds.id = sqlc.arg(build_id)::bigint)
    AND derivation IN
        (SELECT drv_path FROM build_attributes
         WHERE build_id = sqlc.arg(build_id)::bigint
           AND (sqlc.narg(attr)::text IS NULL OR attr = sqlc.narg(attr)))
), reset_attrs AS (
    UPDATE build_attributes SET status = 'pending', error = NULL,
        started_at = NULL, finished_at = NULL
    WHERE build_id = sqlc.arg(build_id)::bigint
      AND (sqlc.narg(attr)::text IS NULL OR attr = sqlc.narg(attr))
), reset_effect_rows AS (
    UPDATE build_effects SET status = 'pending', error = NULL,
        finished_at = NULL, log_path = NULL, log_size = 0,
        log_truncated = FALSE
    WHERE build_id = sqlc.arg(build_id)::bigint
      AND sqlc.narg(attr)::text IS NULL
)
UPDATE builds SET status = 'pending', error = NULL,
    eval_warnings = NULL, started_at = NULL, finished_at = NULL,
    effects_started = CASE WHEN sqlc.narg(attr)::text IS NULL
        THEN FALSE ELSE effects_started END
WHERE builds.id = sqlc.arg(build_id)::bigint;

-- name: ResetEffectsState :exec
-- Drop the started-flag and reset the effect rows atomically (a
-- crash between the two writes must not leave re-runnable effects
-- behind a still-set flag).
WITH flag AS (
    UPDATE builds SET effects_started = FALSE WHERE id = sqlc.arg(build_id)
)
UPDATE build_effects SET status = 'pending', error = NULL,
    finished_at = NULL, log_path = NULL, log_size = 0,
    log_truncated = FALSE WHERE build_id = sqlc.arg(build_id);

-- name: CountUnfinishedAttributes :one
SELECT count(*) AS count FROM build_attributes
WHERE build_id = $1 AND status IN ('pending', 'building');

-- name: ResetEvalForReeval :exec
-- Stale rows (e.g. failed_eval with NULL drv_path) would wedge the
-- aggregate; the re-eval rewrites them. Finished rows with a drv_path
-- are kept: their results are valid and the re-eval skips
-- already-built attributes. One atomic statement: the
-- eval_completed flag must never be observable as set while the
-- partial row set already lost entries (a concurrent build of the
-- same tree would reuse the incomplete eval).
WITH flag AS (
    UPDATE builds SET eval_completed = FALSE WHERE id = sqlc.arg(build_id)
)
DELETE FROM build_attributes WHERE build_id = sqlc.arg(build_id)
AND (status IN ('pending', 'building') OR drv_path IS NULL);

-- name: DeleteAttributesByName :exec
DELETE FROM build_attributes WHERE build_id = $1
AND attr = ANY(sqlc.arg(attrs)::text[]);

-- name: FindUnfinishedBuilds :many
SELECT * FROM builds WHERE status = ANY(sqlc.arg(statuses)::text[])
AND (sqlc.narg(build_id)::bigint IS NULL OR id = sqlc.narg(build_id))
ORDER BY id;

-- name: AttributesForBuilds :many
SELECT build_id, attr, system, drv_path, outputs, status
FROM build_attributes WHERE build_id = ANY(sqlc.arg(build_ids)::bigint[]);

-- name: FailInterruptedEffects :exec
UPDATE build_effects SET status = 'failed',
    error = 'interrupted by a service restart', finished_at = now()
WHERE status = 'running' AND started_at < sqlc.arg(started_before);

-- name: FailInterruptedScheduledRuns :exec
UPDATE scheduled_effect_runs SET status = 'failed',
    error = 'interrupted by a service restart', finished_at = now()
WHERE status = 'running' AND started_at < sqlc.arg(started_before);

-- name: CleanupOldRows :many
-- One retention sweep: builds (cascading to attributes/log rows),
-- scheduled-effect runs, and the per-revision caches (their rows are
-- otherwise only removed on a success flip or explicit rebuild and
-- accumulate forever). Returns the deleted build/run ids so the
-- caller can remove the matching log files.
WITH del_builds AS (
    DELETE FROM builds
    WHERE finished_at IS NOT NULL
      AND finished_at < now() - make_interval(days => sqlc.arg(retention_days)::int)
      -- A restarted build keeps its old finished_at until it
      -- re-aggregates; never delete a build that is running again.
      AND status IN ('succeeded', 'failed', 'cancelled')
    RETURNING builds.id
), del_runs AS (
    DELETE FROM scheduled_effect_runs
    WHERE finished_at IS NOT NULL
      AND finished_at < now() - make_interval(days => sqlc.arg(retention_days)::int)
    RETURNING scheduled_effect_runs.id
), pruned_statuses AS (
    DELETE FROM failed_statuses
    WHERE to_timestamp(timestamp)
        < now() - make_interval(days => sqlc.arg(retention_days)::int)
), pruned_failures AS (
    DELETE FROM failed_builds
    WHERE to_timestamp(timestamp)
        < now() - make_interval(days => sqlc.arg(retention_days)::int)
), pruned_check_runs AS (
    DELETE FROM check_runs
    WHERE to_timestamp(timestamp)
        < now() - make_interval(days => sqlc.arg(retention_days)::int)
)
SELECT 'build' AS kind, del_builds.id FROM del_builds
UNION ALL
SELECT 'scheduled_run' AS kind, del_runs.id FROM del_runs;

-- name: AllBuildIds :many
SELECT id FROM builds;

-- name: SupersedePendingChanges :exec
UPDATE work_queue SET status = 'done', finished_at = now()
WHERE kind = 'change' AND status = 'pending'
  AND payload->>'forge' = sqlc.arg(forge)::text
  AND payload->>'forge_repo_id' = sqlc.arg(forge_repo_id)::text
  AND (payload->>'pr_number')::int = sqlc.arg(pr_number)::int;

-- name: CancelAttribute :execrows
UPDATE build_attributes SET status = 'cancelled', finished_at = now()
WHERE build_id = $1 AND attr = $2 AND status IN ('pending', 'building');

-- name: CancelBuild :one
-- Cancels the build and settles its leftover pending/building
-- attribute rows (they would look running forever) in one statement.
WITH cancelled AS (
    UPDATE builds SET status = 'cancelled', finished_at = now(),
        status_generation = status_generation + 1
    WHERE builds.id = $1 AND builds.status IN ('pending', 'evaluating', 'building')
    RETURNING builds.id, builds.status_generation
), settled AS (
    UPDATE build_attributes SET status = 'cancelled', finished_at = now()
    WHERE build_attributes.build_id IN (SELECT cancelled.id FROM cancelled)
      AND build_attributes.status IN ('pending', 'building')
)
SELECT cancelled.status_generation FROM cancelled;

-- name: DropRemovedEffects :exec
DELETE FROM build_effects WHERE build_id = $1
AND NOT (name = ANY(sqlc.arg(names)::text[]));

-- name: EffectStatus :one
SELECT status FROM build_effects WHERE build_id = $1 AND name = $2;

-- name: SucceededAttributeOutputs :many
SELECT attr, outputs FROM build_attributes
WHERE build_id = $1 AND status IN ('succeeded', 'skipped_local');

-- name: ProjectHasBuilds :one
SELECT 1 AS one FROM builds WHERE project_id = $1
AND status != 'cancelled' LIMIT 1;

-- name: CommitBuilt :one
SELECT 1 AS one FROM builds WHERE project_id = $1 AND commit_sha = $2 LIMIT 1;

-- name: RunningBuildIds :many
SELECT id FROM builds WHERE status IN ('pending', 'evaluating', 'building');
