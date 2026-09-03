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
        started_at = NULL, finished_at = NULL, log_size = 0,
        log_truncated = FALSE
    WHERE build_id = sqlc.arg(build_id)::bigint
      AND (sqlc.narg(attr)::text IS NULL OR attr = sqlc.narg(attr))
), reset_effect_rows AS (
    UPDATE effect_runs SET status = 'pending', error = NULL,
        finished_at = NULL, log_size = 0, log_truncated = FALSE
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
-- behind a still-set flag). A NULL names resets every row. Otherwise
-- only the named effects are reset.
WITH flag AS (
    UPDATE builds SET effects_started = FALSE WHERE id = sqlc.arg(build_id)
)
UPDATE effect_runs SET status = 'pending', error = NULL,
    finished_at = NULL, log_size = 0,
    log_truncated = FALSE
WHERE build_id = sqlc.arg(build_id)
  AND (sqlc.narg(names)::text[] IS NULL OR name = ANY(sqlc.narg(names)::text[]));

-- name: CountUnfinishedAttributes :one
SELECT count(*) AS count FROM build_attributes
WHERE build_id = $1 AND status IN ('pending', 'building');

-- name: CommitEvalResult :exec
-- The only place the attribute set shrinks. Refreshes non-terminal
-- rows, prunes rows the eval no longer produced, publishes via
-- eval_completed, atomically.
WITH new_rows AS (
    INSERT INTO build_attributes (build_id, attr, system, drv_path, outputs,
        eval_warnings, eval_wall_ms, eval_alloc_bytes, status)
    SELECT sqlc.arg(build_id)::bigint, u.attr, u.system, u.drv_path, u.outputs,
        NULLIF(u.eval_warnings, 'null'::jsonb), NULLIF(u.eval_wall_ms, -1), NULLIF(u.eval_alloc_bytes, -1), 'pending'
    FROM (SELECT unnest(sqlc.arg(attrs)::text[]) AS attr,
                 unnest(sqlc.arg(systems)::text[]) AS system,
                 unnest(sqlc.arg(drv_paths)::text[]) AS drv_path,
                 unnest(sqlc.arg(outputs)::jsonb[]) AS outputs,
                 unnest(sqlc.arg(eval_warnings)::jsonb[]) AS eval_warnings,
                 unnest(sqlc.arg(eval_wall_ms)::int[]) AS eval_wall_ms,
                 unnest(sqlc.arg(eval_alloc_bytes)::bigint[]) AS eval_alloc_bytes) u
    ON CONFLICT (build_id, attr) DO UPDATE SET
        system = EXCLUDED.system,
        drv_path = EXCLUDED.drv_path,
        outputs = EXCLUDED.outputs,
        eval_warnings = EXCLUDED.eval_warnings,
        eval_wall_ms = EXCLUDED.eval_wall_ms,
        eval_alloc_bytes = EXCLUDED.eval_alloc_bytes
    WHERE build_attributes.status IN ('pending', 'building')
), pruned AS (
    DELETE FROM build_attributes WHERE build_id = sqlc.arg(build_id)
    AND attr != ALL(sqlc.arg(attrs)::text[])
    AND (status IN ('pending', 'building') OR drv_path IS NULL)
)
UPDATE builds SET eval_completed = TRUE,
    eval_duration_ms = sqlc.narg(eval_duration_ms)::bigint
WHERE builds.id = sqlc.arg(build_id);

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

-- name: FailInterruptedEffects :many
-- build_id is null for schedule runs.
UPDATE effect_runs SET status = 'failed',
    error = 'interrupted by a service restart', finished_at = now()
WHERE status = 'running' AND started_at < sqlc.arg(started_before)
RETURNING build_id, name;

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
    DELETE FROM effect_runs
    WHERE build_id IS NULL AND finished_at IS NOT NULL
      AND finished_at < now() - make_interval(days => sqlc.arg(retention_days)::int)
    RETURNING effect_runs.id
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
SELECT 'effect_run' AS kind, del_runs.id FROM del_runs
UNION ALL
-- Cascade-deleted with their build, still visible in this snapshot.
SELECT 'effect_run' AS kind, r.id FROM effect_runs r
WHERE r.build_id IN (SELECT del_builds.id FROM del_builds);

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
DELETE FROM effect_runs WHERE build_id = $1
AND NOT (name = ANY(sqlc.arg(names)::text[]));

-- name: EffectStatus :one
SELECT status FROM effect_runs WHERE build_id = $1 AND kind = 'push' AND name = $2;

-- name: BuildEffectRunIds :many
-- For unlinking logs on reset. NULL names = all of the build's runs.
SELECT id FROM effect_runs WHERE build_id = sqlc.arg(build_id)
  AND (sqlc.narg(names)::text[] IS NULL OR name = ANY(sqlc.narg(names)::text[]));

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
