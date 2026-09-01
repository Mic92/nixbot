-- onEvent deliveries (deliver.py).

-- name: UpsertEventEffect :one
-- One row per (build, kind, name). A repeated delivery for the same
-- build (e.g. a second /plan comment) resets it like an effects restart.
INSERT INTO effect_runs (project_id, kind, build_id, name, status, skip_reason,
                         payload, code_rev, actor, started_at, finished_at)
SELECT b.project_id, sqlc.arg(kind)::text, b.id, sqlc.arg(name)::text,
       sqlc.arg(status)::text, sqlc.narg(skip_reason)::text,
       sqlc.arg(payload)::jsonb, sqlc.arg(code_rev)::text, sqlc.narg(actor)::text,
       now(), CASE WHEN sqlc.arg(status)::text = 'pending' THEN NULL ELSE now() END
FROM builds b WHERE b.id = sqlc.arg(build_id)::bigint
ON CONFLICT (build_id, kind, name) DO UPDATE SET
    status = EXCLUDED.status, skip_reason = EXCLUDED.skip_reason,
    payload = EXCLUDED.payload, code_rev = EXCLUDED.code_rev,
    actor = EXCLUDED.actor, error = NULL, log_size = 0, log_truncated = FALSE,
    started_at = EXCLUDED.started_at, finished_at = EXCLUDED.finished_at
WHERE effect_runs.status NOT IN ('running')
RETURNING id;

-- name: SupersedeEventEffects :many
-- A newer delivery for the same PR (or branch, without a PR) cancels
-- still-queued effects of older ones. Running ones are left alone.
-- Comments only supersede the same /command: a /ping must not cancel
-- a queued /apply.
WITH cancelled AS (
    UPDATE effect_runs SET status = 'cancelled', finished_at = now(),
        error = 'superseded by a newer event'
    WHERE project_id = sqlc.arg(project_id)::bigint
      AND kind = sqlc.arg(kind)::text
      AND build_id <> sqlc.arg(build_id)::bigint
      AND status = 'pending'
      AND (sqlc.narg(command)::text IS NULL
           OR payload->>'command' = sqlc.narg(command)::text)
      AND CASE WHEN sqlc.narg(pr_number)::bigint IS NULL
          THEN payload->'pullRequest' IS NULL
               AND payload->'build'->>'branch' = sqlc.narg(branch)::text
          ELSE (payload->'pullRequest'->>'number')::bigint = sqlc.narg(pr_number)::bigint
          END
    RETURNING build_id, name
)
UPDATE work_queue w SET status = 'done', finished_at = now()
FROM cancelled c
WHERE w.kind = 'effect' AND w.status = 'pending'
  AND (w.payload->>'build_id')::bigint = c.build_id
  AND w.payload->>'kind' = sqlc.arg(kind)::text
  AND w.payload->>'name' = c.name
RETURNING c.build_id, c.name;

-- name: LatestBuildForPr :one
-- merged_pr_number: the merge push may have taken the build over
-- before the close event arrives.
SELECT * FROM builds
WHERE project_id = $1
  AND sqlc.arg(pr_number)::bigint IN (pr_number, merged_pr_number)
ORDER BY number DESC LIMIT 1;


-- name: PreviousFinishedStatus :one
-- The build before this one for the same branch or PR, for
-- build_finished broke/fixed transitions.
SELECT p.status FROM builds b
JOIN builds p ON p.project_id = b.project_id AND p.number < b.number
  AND p.branch = b.branch AND p.pr_number IS NOT DISTINCT FROM b.pr_number
  AND p.status IN ('succeeded', 'failed')
WHERE b.id = sqlc.arg(build_id)::bigint
ORDER BY p.number DESC LIMIT 1;

-- name: FailedAttrNames :many
SELECT attr FROM build_attributes
WHERE build_id = $1
  AND status IN ('failed', 'failed_eval', 'dependency_failed', 'cached_failure')
ORDER BY attr LIMIT 50;
