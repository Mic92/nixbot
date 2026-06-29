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
                    branch, pr_number, pr_author)
SELECT sqlc.arg(project_id)::bigint, n.number, sqlc.narg(tree_hash)::text,
       sqlc.arg(commit_sha)::text, sqlc.arg(branch)::text,
       sqlc.narg(pr_number)::bigint, sqlc.narg(pr_author)::text
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
-- Batch insert of one eval's attribute rows; arrays are zipped
-- positionally by unnest.
INSERT INTO build_attributes (build_id, attr, system, drv_path, outputs, status)
SELECT sqlc.arg(build_id)::bigint, u.attr, u.system, u.drv_path, u.outputs, 'pending'
FROM (SELECT unnest(sqlc.arg(attrs)::text[]) AS attr,
             unnest(sqlc.arg(systems)::text[]) AS system,
             unnest(sqlc.arg(drv_paths)::text[]) AS drv_path,
             unnest(sqlc.arg(outputs)::jsonb[]) AS outputs) u
ON CONFLICT (build_id, attr) DO NOTHING;

-- name: SetEvalWarnings :exec
UPDATE builds SET eval_warnings = sqlc.arg(warnings)::jsonb WHERE id = $1;

-- name: SetBuildStatus :exec
-- A failed/cancelled build also settles its pending effect rows in
-- the same statement: they only get queue items when the build
-- succeeds; after a failed rebuild nothing else owns them.
WITH failed_effects AS (
    UPDATE build_effects SET status = 'failed',
        error = 'build did not succeed', finished_at = now()
    WHERE build_effects.build_id = sqlc.arg(id)::bigint
      AND build_effects.status = 'pending'
      AND sqlc.arg(status)::text IN ('failed', 'cancelled')
)
UPDATE builds
SET status = sqlc.arg(status),
    error = COALESCE(sqlc.narg(error), error),
    -- A fresh eval must not show the previous attempt's streamed
    -- warnings.
    eval_warnings = CASE
        WHEN sqlc.arg(status) = 'evaluating' THEN NULL
        ELSE eval_warnings
    END,
    -- A fresh eval invalidates the recorded job set until it
    -- completes again.
    eval_completed = CASE
        WHEN sqlc.arg(status) = 'evaluating' THEN FALSE
        ELSE eval_completed
    END,
    started_at = CASE
        WHEN started_at IS NULL AND sqlc.arg(status) <> 'pending' THEN now()
        ELSE started_at
    END,
    -- Invariant: non-terminal states never carry finished_at, else
    -- reruns show negative durations.
    finished_at = CASE
        WHEN sqlc.arg(status) = ANY(sqlc.arg(terminal)::text[]) THEN now()
        ELSE NULL
    END
WHERE builds.id = sqlc.arg(id)::bigint;

-- name: MarkEvalCompleted :exec
UPDATE builds SET eval_completed = TRUE WHERE id = $1;

-- name: FindCompletedEval :one
SELECT id FROM builds WHERE project_id = $1 AND tree_hash = $2
AND eval_completed AND id <> sqlc.arg(exclude_build_id)
ORDER BY id DESC LIMIT 1;

-- name: EvalJobRows :many
SELECT attr, system, drv_path, outputs FROM build_attributes
WHERE build_id = $1;

-- name: GetBuild :one
SELECT * FROM builds WHERE id = $1;

-- name: MarkEffectsStarted :one
-- Records the triggering ref alongside the flag so the effect items
-- (which only carry build_id) report on the commit that ran them
-- rather than the build's stored commit_sha.
UPDATE builds SET effects_started = TRUE,
    effects_commit_sha = sqlc.arg(commit_sha)::text,
    effects_branch = sqlc.arg(branch)::text,
    effects_pr_number = sqlc.narg(pr_number)::bigint
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
-- rows are left untouched (the upsert returns no row, so the log
-- CTEs become no-ops as well).
WITH attr AS (
    INSERT INTO build_attributes
        (build_id, attr, system, drv_path, outputs, status, error,
         cached, finished_at)
    VALUES ($1, $2, $3, $4, sqlc.narg(outputs)::jsonb, $5, $6, $7, now())
    ON CONFLICT (build_id, attr) DO UPDATE SET
        status = EXCLUDED.status,
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
        finished_at = now()
    WHERE NOT sqlc.arg(if_unfinished)::boolean
        OR build_attributes.status IN ('pending', 'building')
    RETURNING build_attributes.id
), dropped AS (
    -- Reruns rewrite the same log file; replace the metadata row
    -- instead of accumulating duplicates.
    DELETE FROM logs WHERE attribute_id IN (SELECT attr.id FROM attr)
      AND sqlc.narg(log_path)::text IS NOT NULL
)
INSERT INTO logs (attribute_id, path, size_bytes, truncated)
SELECT attr.id, sqlc.narg(log_path), sqlc.arg(log_size)::bigint,
       sqlc.arg(log_truncated)::boolean
FROM attr WHERE sqlc.narg(log_path)::text IS NOT NULL;

-- name: StartEffect :exec
INSERT INTO build_effects (build_id, name, status) VALUES ($1, $2, $3)
ON CONFLICT (build_id, name) DO UPDATE SET
    status = $3, error = NULL, log_path = NULL, log_size = 0,
    log_truncated = FALSE, started_at = now(), finished_at = NULL;

-- name: StartPendingEffects :exec
-- Batch variant for enqueueing one build's discovered effects.
INSERT INTO build_effects (build_id, name, status)
SELECT sqlc.arg(build_id)::bigint, u.name, 'pending'
FROM unnest(sqlc.arg(names)::text[]) AS u(name)
ON CONFLICT (build_id, name) DO UPDATE SET
    status = 'pending', error = NULL, log_path = NULL, log_size = 0,
    log_truncated = FALSE, started_at = now(), finished_at = NULL;

-- name: FinishEffect :exec
UPDATE build_effects SET
    status = $3, error = sqlc.narg(error), log_path = sqlc.narg(log_path),
    log_size = $4, log_truncated = $5, finished_at = now()
WHERE build_id = $1 AND name = $2;

-- name: EffectsForBuild :many
SELECT * FROM build_effects WHERE build_id = $1 ORDER BY name;

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
