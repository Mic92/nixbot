-- Scheduled effects (scheduled.py).

-- name: SchedulesForUpdate :many
SELECT schedule_name, effect, when_spec, last_run
FROM scheduled_effects WHERE project_id = $1;

-- name: DeleteProjectSchedules :exec
DELETE FROM scheduled_effects WHERE project_id = $1;

-- name: InsertSchedules :exec
-- Batch insert of a project's freshly discovered schedules; arrays
-- are zipped positionally by unnest.
INSERT INTO scheduled_effects
    (project_id, schedule_name, effect, when_spec, last_run)
SELECT sqlc.arg(project_id)::bigint, u.schedule_name, u.effect, u.when_spec,
       u.last_run
FROM (SELECT unnest(sqlc.arg(schedule_names)::text[]) AS schedule_name,
             unnest(sqlc.arg(effects)::text[]) AS effect,
             unnest(sqlc.arg(when_specs)::jsonb[]) AS when_spec,
             unnest(sqlc.arg(last_runs)::timestamptz[]) AS last_run) u;

-- name: DueScheduleRows :many
SELECT project_id, schedule_name, effect, when_spec, last_run
FROM scheduled_effects
WHERE last_run IS NULL
   OR last_run < date_trunc('minute', sqlc.arg(now)::timestamptz);

-- name: StartScheduledRun :one
INSERT INTO effect_runs (project_id, kind, schedule_name, name)
VALUES ($1, 'schedule', $2, $3) RETURNING id;

-- name: FinishScheduledRun :exec
UPDATE effect_runs
SET status = $2, error = sqlc.narg(error), log_size = $3, log_truncated = $4,
    finished_at = now()
WHERE id = $1;

-- name: LatestScheduledRuns :many
SELECT DISTINCT ON (schedule_name, name)
       id, schedule_name, name AS effect, status, error,
       started_at, finished_at
FROM effect_runs WHERE project_id = $1 AND kind = 'schedule'
ORDER BY schedule_name, name, started_at DESC;

-- name: ProjectSchedules :many
SELECT schedule_name, effect, when_spec, last_run
FROM scheduled_effects WHERE project_id = $1
ORDER BY schedule_name, effect;

-- name: MarkScheduleRun :exec
UPDATE scheduled_effects SET last_run = $4
WHERE project_id = $1 AND schedule_name = $2 AND effect = $3;

-- name: ScheduledRunsForEffect :many
-- Run history for one (schedule, effect); cursor on id for infinite
-- scroll (id order matches started_at order for a single effect).
SELECT id, schedule_name, name AS effect, status, error, started_at, finished_at
FROM effect_runs
WHERE project_id = sqlc.arg(project_id)
  AND kind = 'schedule'
  AND schedule_name = sqlc.arg(schedule_name)
  AND name = sqlc.arg(effect)
  AND (sqlc.narg(before)::bigint IS NULL OR id < sqlc.narg(before))
ORDER BY id DESC LIMIT sqlc.arg(limit_)::bigint;
