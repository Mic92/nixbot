-- Build lifecycle queries (db.py).

-- name: LockBuildIdentity :exec
-- No unique constraint exists on (project_id, tree_hash); serialize
-- creators or concurrent events insert duplicates.
SELECT pg_advisory_xact_lock(hashtextextended(sqlc.arg(key)::text, 0));

-- name: FindReusableBuild :one
-- A cancelled build carries no verdict; never reuse it.
SELECT * FROM builds WHERE project_id = $1 AND tree_hash = $2
AND status <> 'cancelled' ORDER BY id DESC LIMIT 1;

-- name: DetachBuildFromPr :one
-- Reused in another context (another PR, or the default branch after
-- the PR merged): drop number and author together so the stale PR
-- keeps no authz, and let a plain branch push take over the branch.
UPDATE builds SET pr_number = NULL, pr_author = NULL,
    merged_pr_number = CASE WHEN sqlc.narg(pr_number)::bigint IS NULL
             THEN builds.pr_number ELSE NULL END,
    branch = CASE WHEN sqlc.narg(pr_number)::bigint IS NULL
             THEN sqlc.arg(branch) ELSE branch END
WHERE id = $1 RETURNING *;

-- name: AttachBuildToPr :one
UPDATE builds SET pr_number = $2, pr_author = $3 WHERE id = $1 RETURNING *;

-- name: BackfillPrAuthor :one
UPDATE builds SET pr_author = $2 WHERE id = $1 RETURNING *;

-- name: CreateBuild :one
-- Claims the project's next build number and inserts the build in
-- one atomic statement.
WITH n AS (
    UPDATE projects SET next_build_number = next_build_number + 1
    WHERE id = sqlc.arg(project_id)::bigint
    RETURNING next_build_number - 1 AS number
)
INSERT INTO builds (project_id, number, tree_hash, commit_sha,
                    branch, pr_number, pr_author, actor)
SELECT sqlc.arg(project_id)::bigint, n.number, sqlc.narg(tree_hash)::text,
       sqlc.arg(commit_sha)::text, sqlc.arg(branch)::text,
       sqlc.narg(pr_number)::bigint, sqlc.narg(pr_author)::text,
       sqlc.narg(actor)::text
FROM n
RETURNING *;

-- name: CreateFailedBuild :one
WITH n AS (
    UPDATE projects SET next_build_number = next_build_number + 1
    WHERE id = sqlc.arg(project_id)::bigint
    RETURNING next_build_number - 1 AS number
)
INSERT INTO builds (project_id, number, commit_sha, branch,
                    pr_number, pr_author, status, error, finished_at)
SELECT sqlc.arg(project_id)::bigint, n.number, sqlc.arg(commit_sha)::text,
       sqlc.arg(branch)::text, sqlc.narg(pr_number)::bigint,
       sqlc.narg(pr_author)::text, 'failed', sqlc.narg(error)::text, now()
FROM n
RETURNING *;

-- name: RecordAttributes :exec
-- Streaming inserts while the eval runs. Rows only grow or refresh
-- here, they shrink solely in CommitEvalResult.
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
WHERE build_attributes.status IN ('pending', 'building');

-- name: SetEvalWarnings :exec
UPDATE builds SET eval_warnings = sqlc.arg(warnings)::jsonb WHERE id = $1;

-- name: SetBuildStatus :exec
UPDATE builds
SET status = sqlc.arg(status),
    -- Every run starts at pending. Drop the previous attempt's
    -- error, warnings, job set and duration.
    error = CASE
        WHEN sqlc.arg(status) = 'pending' THEN NULL
        ELSE COALESCE(sqlc.narg(error), error)
    END,
    eval_duration_ms = CASE
        WHEN sqlc.arg(status) = 'pending' THEN NULL ELSE eval_duration_ms
    END,
    eval_warnings = CASE
        WHEN sqlc.arg(status) = 'pending' THEN NULL ELSE eval_warnings
    END,
    eval_completed = CASE
        WHEN sqlc.arg(status) = 'pending' THEN FALSE ELSE eval_completed
    END,
    started_at = CASE
        WHEN sqlc.arg(status) = 'pending' THEN NULL
        WHEN started_at IS NULL
            AND sqlc.arg(status) IN ('evaluating', 'building') THEN now()
        ELSE started_at
    END,
    -- Invariant: non-terminal states never carry finished_at, else
    -- reruns show negative durations.
    finished_at = CASE
        WHEN sqlc.arg(status) = ANY(sqlc.arg(terminal)::text[]) THEN now()
        ELSE NULL
    END
WHERE builds.id = sqlc.arg(id)::bigint;

-- name: FindCompletedEval :one
SELECT id FROM builds WHERE project_id = $1 AND tree_hash = $2
AND eval_completed AND id <> sqlc.arg(exclude_build_id)
ORDER BY id DESC LIMIT 1;

-- name: EvalJobRows :many
SELECT attr, system, drv_path, outputs FROM build_attributes
WHERE build_id = $1;

-- name: GetBuild :one
SELECT * FROM builds WHERE id = $1;

-- name: SetBuildAttributePrefix :exec
INSERT INTO build_reporting (build_id, attribute_prefix)
VALUES ($1, $2)
ON CONFLICT (build_id) DO UPDATE
SET attribute_prefix = EXCLUDED.attribute_prefix;

-- name: BuildAttributePrefix :one
SELECT attribute_prefix FROM build_reporting WHERE build_id = $1;

-- name: RecordBuildReportTarget :exec
WITH reporting AS (
    INSERT INTO build_reporting (build_id) VALUES ($1)
    ON CONFLICT (build_id) DO UPDATE SET reported_generation = NULL
    RETURNING build_id
)
INSERT INTO build_report_targets (build_id, commit_sha, branch, pr_number)
SELECT build_id, $2, $3, $4 FROM reporting
ON CONFLICT (build_id, commit_sha) DO UPDATE
SET branch = EXCLUDED.branch, pr_number = EXCLUDED.pr_number;

-- name: BuildReportTargets :many
SELECT commit_sha, branch, pr_number FROM build_report_targets
WHERE build_id = $1 ORDER BY commit_sha;

-- name: MarkBuildReportDelivered :exec
UPDATE build_reporting r
SET reported_generation = sqlc.arg(generation)::bigint
FROM builds b
WHERE r.build_id = sqlc.arg(build_id)::bigint
  AND b.id = r.build_id
  AND b.status_generation = sqlc.arg(generation)::bigint
  AND b.status IN ('succeeded', 'failed', 'cancelled')
  -- A target attached while forge calls were in flight must keep this
  -- generation unreconciled; the caller's snapshot did not include it.
  AND NOT EXISTS (
      SELECT 1 FROM build_report_targets t
      WHERE t.build_id = b.id
        AND NOT (t.commit_sha = ANY(sqlc.arg(commit_shas)::text[]))
  );

-- name: UnreconciledTerminalBuilds :many
SELECT b.id
FROM builds b
JOIN build_reporting r ON r.build_id = b.id
WHERE b.status IN ('succeeded', 'failed', 'cancelled')
  AND r.reported_generation IS DISTINCT FROM b.status_generation
  AND NOT EXISTS (
      SELECT 1 FROM work_queue w
      WHERE w.kind = 'report'
        AND w.status IN ('pending', 'running')
        AND (w.payload->>'build_id')::bigint = b.id
  )
ORDER BY b.id;

-- name: AttributeForReport :one
SELECT a.attr, a.status, a.error, a.system, a.drv_path, a.finished_at,
       b.status AS build_status, b.status_generation,
       COALESCE(r.attribute_prefix, 'checks') AS attribute_prefix
FROM build_attributes a
JOIN builds b ON b.id = a.build_id
LEFT JOIN build_reporting r ON r.build_id = b.id
WHERE a.build_id = $1 AND a.attr = $2;

-- name: AttributeReportRows :many
SELECT attr, status, error, system, drv_path FROM build_attributes
WHERE build_id = $1 ORDER BY attr;

-- name: ReportableAttributeFailures :many
WITH ranked AS (
    SELECT a.build_id, a.attr,
           row_number() OVER (PARTITION BY a.build_id ORDER BY a.attr) AS ordinal
    FROM build_attributes a
    JOIN build_reporting r ON r.build_id = a.build_id
    WHERE a.status IN ('failed', 'failed_eval', 'dependency_failed', 'cached_failure')
)
SELECT build_id, attr FROM ranked
WHERE ordinal <= sqlc.arg(report_limit)::bigint
ORDER BY build_id, attr;

-- name: RecordEffectsRef :exec
-- The ref maybe_run_effects last decided for. Effect items (which only
-- carry build_id) report on it, and restarts gate on it instead of the
-- build's own ref. An allowed ref replaces a gated one, not vice versa.
UPDATE builds SET
    effects_commit_sha = sqlc.arg(commit_sha)::text,
    effects_branch = sqlc.arg(branch)::text,
    effects_pr_number = sqlc.narg(pr_number)::bigint
WHERE id = sqlc.arg(id)::bigint
  AND (effects_commit_sha IS NULL OR sqlc.arg(allowed)::boolean);

-- name: MarkEffectsStarted :one
UPDATE builds SET effects_started = TRUE
WHERE id = sqlc.arg(id)::bigint AND effects_started = FALSE RETURNING id;

-- name: SettleUnfinishedAttributes :exec
UPDATE build_attributes
SET status = 'cancelled', finished_at = now()
WHERE build_id = $1 AND status IN ('pending', 'building');

-- name: MarkAttributeBuilding :one
INSERT INTO build_attributes
    (build_id, attr, system, drv_path, status, started_at)
VALUES ($1, $2, $3, $4, 'building', now())
ON CONFLICT (build_id, attr) DO UPDATE SET
    status = 'building',
    started_at = now(),
    finished_at = NULL
WHERE build_attributes.status IN ('pending', 'building')
RETURNING attr;

-- name: CompleteAttribute :exec
-- Status, outputs, error and log metadata in one atomic statement
-- (crash-recovery invariant). With if_unfinished, already-terminal
-- rows are left untouched (the upsert returns no row).
INSERT INTO build_attributes
    (build_id, attr, system, drv_path, outputs, status, error,
     cached, log_size, log_truncated, finished_at,
     eval_warnings, eval_wall_ms, eval_alloc_bytes)
VALUES ($1, $2, $3, $4, sqlc.narg(outputs)::jsonb, $5, $6, $7,
        sqlc.arg(log_size)::bigint, sqlc.arg(log_truncated)::boolean, now(),
        sqlc.narg(eval_warnings)::jsonb, sqlc.narg(eval_wall_ms)::int,
        sqlc.narg(eval_alloc_bytes)::bigint)
ON CONFLICT (build_id, attr) DO UPDATE SET
    status = EXCLUDED.status,
    -- Eval data is only known by failed_eval inserts. Build
    -- completions pass NULL and must keep what the eval recorded.
    eval_warnings = COALESCE(EXCLUDED.eval_warnings, build_attributes.eval_warnings),
    eval_wall_ms = COALESCE(EXCLUDED.eval_wall_ms, build_attributes.eval_wall_ms),
    eval_alloc_bytes = COALESCE(EXCLUDED.eval_alloc_bytes, build_attributes.eval_alloc_bytes),
    -- Eval recorded the full outputs map (multi-output drvs);
    -- merge the freshly-known "out" path into it instead of
    -- replacing it, and never NULL an existing map when no out
    -- path is known.
    outputs = CASE
        WHEN EXCLUDED.outputs IS NULL
            THEN build_attributes.outputs
        ELSE COALESCE(build_attributes.outputs, '{}'::jsonb)
            || EXCLUDED.outputs
    END,
    error = EXCLUDED.error,
    cached = EXCLUDED.cached,
    log_size = EXCLUDED.log_size,
    log_truncated = EXCLUDED.log_truncated,
    finished_at = now()
WHERE NOT sqlc.arg(if_unfinished)::boolean
    OR build_attributes.status IN ('pending', 'building');

-- name: ClaimEffect :one
-- A queue item takes its pending row. No row when a restart deleted it
-- or another item got there first.
UPDATE effect_runs SET status = sqlc.arg(status)::text, started_at = now()
WHERE build_id = sqlc.arg(build_id)::bigint AND kind = sqlc.arg(kind)::text
  AND name = sqlc.arg(name)::text AND status = 'pending'
RETURNING id;

-- name: InsertBuildEffects :exec
-- The only producer of pending build-owned rows. Restarts delete rows
-- first, so a conflict means the row belongs to a live or finished run
-- (a gated ref reusing a build) and stays.
INSERT INTO effect_runs (project_id, kind, build_id, name, status, deps, finished_at)
SELECT b.project_id, sqlc.arg(kind)::text, b.id, u.name, sqlc.arg(status)::text,
       u.deps::jsonb,
       CASE WHEN sqlc.arg(status)::text = 'pending' THEN NULL ELSE now() END
FROM builds b,
     (SELECT unnest(sqlc.arg(names)::text[]) AS name,
             unnest(sqlc.arg(deps)::text[]) AS deps) AS u
WHERE b.id = sqlc.arg(build_id)::bigint
ON CONFLICT (build_id, kind, name) DO NOTHING;

-- name: DropRemovedChecks :exec
DELETE FROM effect_runs
WHERE build_id = sqlc.arg(build_id)::bigint AND kind = 'check'
  AND NOT (name = ANY(sqlc.arg(names)::text[]));

-- name: EffectDepStatuses :many
-- Statuses of the effects this effect declared in `after` (onPush only).
SELECT d.name, d.status FROM effect_runs e
JOIN effect_runs d ON d.build_id = e.build_id AND d.kind = 'push'
    AND d.name IN (SELECT jsonb_array_elements_text(e.deps))
WHERE e.build_id = $1 AND e.kind = 'push' AND e.name = $2;

-- name: FinishEffect :exec
-- Only a live row: a cancel that raced the run keeps its verdict.
UPDATE effect_runs SET
    status = sqlc.arg(status), error = sqlc.narg(error),
    log_size = sqlc.arg(log_size), log_truncated = sqlc.arg(log_truncated),
    finished_at = now()
WHERE build_id = sqlc.arg(build_id) AND kind = sqlc.arg(kind) AND name = sqlc.arg(name)
  AND status IN ('pending', 'running', 'dependency_failed');

-- name: EffectsSummary :one
-- Running while anything is in flight, else the worst outcome. Eval
-- errors of the built commit count as failures. No row when empty.
WITH rows AS (
    SELECT r.status FROM effect_runs r
    WHERE r.build_id = sqlc.arg(build_id)::bigint AND r.owner = 'build'
    UNION ALL
    SELECT 'failed' FROM effect_eval_errors x
    WHERE x.build_id = sqlc.arg(build_id)::bigint AND x.source <> 'delivery'
)
SELECT
    count(*) FILTER (WHERE status IN ('failed', 'dependency_failed'))::bigint AS failed,
    count(*) FILTER (WHERE status = 'succeeded')::bigint AS succeeded,
    CASE
        WHEN bool_or(status = 'running')
          OR (bool_or(status = 'pending') AND NOT bool_and(status = 'pending'))
          THEN 'running'
        WHEN bool_or(status IN ('failed', 'dependency_failed')) THEN 'failed'
        WHEN bool_or(status = 'pending') THEN 'pending'
        WHEN bool_or(status = 'succeeded') THEN 'succeeded'
        ELSE 'skipped'
    END::text AS status
FROM rows
HAVING count(*) > 0;

-- name: RecordEffectEvalError :exec
INSERT INTO effect_eval_errors (build_id, source, error, code_rev)
VALUES ($1, $2, sqlc.arg(error)::text, sqlc.narg(code_rev)::text)
ON CONFLICT (build_id, source) DO UPDATE SET
    error = EXCLUDED.error, code_rev = EXCLUDED.code_rev, created_at = now();

-- name: ClearEffectEvalError :exec
DELETE FROM effect_eval_errors WHERE build_id = $1 AND source = $2;

-- name: EffectEvalErrors :many
SELECT * FROM effect_eval_errors WHERE build_id = $1 ORDER BY source;

-- name: EffectsForBuild :many
SELECT * FROM effect_runs WHERE build_id = $1 ORDER BY name;

-- name: AttributeStatuses :many
SELECT attr, status FROM build_attributes WHERE build_id = $1;

-- name: LockBuildRow :one
-- Aggregation must lock the row BEFORE reading the attribute
-- statuses: a single UPDATE with an aggregate CTE would compute the
-- verdict from a snapshot taken before the lock was granted, so a
-- concurrent restart's attribute reset could be missed and a stale
-- "succeeded" written over the requeued build.
SELECT id, status, status_generation FROM builds WHERE id = $1 FOR UPDATE;

-- name: AttributeStatusList :many
SELECT status FROM build_attributes WHERE build_id = $1;

-- name: BumpBuildStatus :one
UPDATE builds
SET status = $2,
    status_generation = status_generation + 1,
    finished_at = COALESCE(finished_at, now())
WHERE id = $1
RETURNING status_generation;
