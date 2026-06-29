-- Read queries for the web UI (web/queries.py, web/logs.py,
-- web/metrics.py, web/control_routes.py). Queries with dynamically
-- assembled WHERE/ORDER clauses stay in web/queries.py.

-- name: WebProjects :many
SELECT * FROM projects
WHERE (sqlc.narg(enabled)::boolean IS NULL OR enabled = sqlc.narg(enabled))
  AND (sqlc.narg(pattern)::text IS NULL OR owner || '/' || name ILIKE sqlc.narg(pattern))
ORDER BY owner, name;

-- name: WebRepo :one
SELECT * FROM projects WHERE forge = $1 AND owner = $2 AND name = $3;

-- name: WebRepoCandidates :many
SELECT * FROM projects WHERE owner = $1 AND name = $2
ORDER BY forge, id;

-- name: WebRepoOverview :many
SELECT p.*,
       lb.number AS last_number, lb.status AS last_status,
       lb.branch AS last_branch, lb.created_at AS last_created_at,
       lb.started_at, lb.finished_at,
       h.history, m.median_secs, m.pass_rate
FROM projects p
LEFT JOIN LATERAL (
    SELECT * FROM builds b WHERE b.project_id = p.id
    ORDER BY b.number DESC LIMIT 1
) lb ON true
LEFT JOIN LATERAL (
    SELECT json_agg(
        json_build_object(
            'number', t.number, 'status', t.status,
            'secs', EXTRACT(EPOCH FROM (t.finished_at - t.started_at))
        )
        ORDER BY t.number
    ) AS history
    FROM (
        SELECT number, status, started_at, finished_at FROM builds b
        WHERE b.project_id = p.id
          AND b.pr_number IS NULL AND b.branch = p.default_branch
        ORDER BY number DESC LIMIT 10
    ) t
) h ON true
LEFT JOIN LATERAL (
    -- Median, not mean: one build stuck behind a busy nix daemon must
    -- not dominate the typical duration.
    SELECT percentile_cont(0.5) WITHIN GROUP (
               ORDER BY EXTRACT(EPOCH FROM (t.finished_at - t.started_at))
           ) FILTER (WHERE t.status = 'succeeded') AS median_secs,
           -- Cancelled builds say nothing about the code: they must
           -- not drag the pass rate toward zero.
           count(*) FILTER (WHERE t.status = 'succeeded')::float
               / NULLIF(
                   count(*) FILTER (
                       WHERE t.status IN ('succeeded', 'failed')
                   ), 0) AS pass_rate
    FROM (
        SELECT status, started_at, finished_at FROM builds b
        WHERE b.project_id = p.id
          AND b.pr_number IS NULL AND b.branch = p.default_branch
          AND b.status IN ('succeeded', 'failed', 'cancelled')
        ORDER BY b.number DESC LIMIT 30
    ) t
) m ON true
WHERE p.enabled AND (sqlc.narg(project_ids)::bigint[] IS NULL OR p.id = ANY(sqlc.narg(project_ids)))
  AND (sqlc.narg(pattern)::text IS NULL OR p.owner || '/' || p.name ILIKE sqlc.narg(pattern))
ORDER BY p.owner, p.name;

-- name: WebRecentBuilds :many
SELECT b.*, p.owner, p.name AS project_name, p.forge, p.url
FROM builds b JOIN projects p ON p.id = b.project_id
WHERE (sqlc.narg(project_ids)::bigint[] IS NULL OR b.project_id = ANY(sqlc.narg(project_ids)))
  AND (sqlc.narg(before)::bigint IS NULL OR b.id < sqlc.narg(before))
ORDER BY b.id DESC LIMIT sqlc.arg(limit_)::bigint;
-- name: WebBuildsForRepo :many
-- Build list page with optional filters; commit is a prefix match so
-- agents can pass short revs. Fetches limit+1 rows for has_next.
SELECT * FROM builds
WHERE project_id = $1
  AND (sqlc.narg(status)::text IS NULL OR status = sqlc.narg(status))
  AND (sqlc.narg(branch)::text IS NULL OR branch = sqlc.narg(branch))
  AND (sqlc.narg(pr_number)::int IS NULL OR pr_number = sqlc.narg(pr_number))
  AND (sqlc.narg(commit_prefix)::text IS NULL
       OR starts_with(commit_sha, sqlc.narg(commit_prefix)))
  AND (sqlc.narg(before)::bigint IS NULL OR id < sqlc.narg(before))
ORDER BY number DESC LIMIT sqlc.arg('limit') OFFSET sqlc.arg('offset');

-- name: WebBuildByNumber :one
SELECT * FROM builds WHERE project_id = $1 AND number = $2;

-- name: WebNeighborNumbers :one
SELECT (max(number) FILTER (WHERE number < sqlc.arg(number)))::bigint AS prev,
       (min(number) FILTER (WHERE number > sqlc.arg(number)))::bigint AS next
FROM builds WHERE project_id = $1;

-- name: WebAttributes :many
SELECT a.*, l.path AS log_path, l.size_bytes AS log_size
FROM build_attributes a
LEFT JOIN logs l ON l.attribute_id = a.id
WHERE a.build_id = $1
-- Display order: failures first, then running, then the rest.
ORDER BY CASE
    WHEN a.status IN ('failed', 'failed_eval', 'dependency_failed',
                      'cached_failure') THEN 0
    WHEN a.status = 'building' THEN 1
    WHEN a.status = 'pending' THEN 2
    WHEN a.status = 'cancelled' THEN 3
    ELSE 4
END, a.attr;

-- name: WebEffects :many
SELECT * FROM build_effects WHERE build_id = $1 ORDER BY name;

-- name: WebAttributeCounts :many
SELECT status, count(*) AS count FROM build_attributes
WHERE build_id = $1 AND (sqlc.narg(pattern)::text IS NULL OR attr ILIKE sqlc.narg(pattern))
GROUP BY status;

-- name: WebAttributePage :many
SELECT a.*, l.path AS log_path, l.size_bytes AS log_size
FROM build_attributes a
LEFT JOIN logs l ON l.attribute_id = a.id
WHERE a.build_id = $1 AND a.status = ANY(sqlc.arg(statuses)::text[])
  AND (sqlc.narg(pattern)::text IS NULL OR a.attr ILIKE sqlc.narg(pattern))
ORDER BY a.attr LIMIT sqlc.arg(limit_)::bigint OFFSET sqlc.arg(offset_)::bigint;

-- name: WebAttributeHistory :many
SELECT a.*, b.number AS build_number, b.branch, b.commit_sha,
       b.created_at AS build_created_at
FROM build_attributes a
JOIN builds b ON b.id = a.build_id
WHERE b.project_id = $1 AND a.attr = $2
ORDER BY b.number DESC LIMIT sqlc.arg(limit_)::bigint;

-- name: WebAttributeNeighborNumbers :one
SELECT (max(b.number) FILTER (WHERE b.number < sqlc.arg(build_number)))::bigint AS prev,
       (min(b.number) FILTER (WHERE b.number > sqlc.arg(build_number)))::bigint AS next
FROM build_attributes a
JOIN builds b ON b.id = a.build_id
WHERE b.project_id = $1 AND a.attr = $2;

-- name: WebQueue :many
-- queue_position numbers the GLOBAL queue of pending builds: computed
-- before any visibility filter so every viewer sees the same
-- position, and NULL for already-running builds.
SELECT b.*, p.owner, p.name AS project_name, p.forge, p.url,
       q.queue_position
FROM builds b
JOIN projects p ON p.id = b.project_id
LEFT JOIN (
    SELECT id, row_number() OVER (ORDER BY id) AS queue_position
    FROM builds WHERE status = 'pending'
) q ON q.id = b.id
WHERE b.status IN ('pending', 'evaluating', 'building')
  AND (sqlc.narg(project_ids)::bigint[] IS NULL OR p.id = ANY(sqlc.narg(project_ids)))
-- Active builds first; queue_position stays FIFO.
ORDER BY b.status = 'pending', b.id;

-- name: EffectLogPath :one
SELECT log_path AS path FROM build_effects
WHERE build_id = $1 AND name = $2 AND log_path IS NOT NULL;

-- name: AttributeLogPath :one
SELECT l.path FROM logs l
JOIN build_attributes a ON a.id = l.attribute_id
WHERE a.build_id = $1 AND a.attr = $2;

-- name: AttributeStatus :one
SELECT status FROM build_attributes WHERE build_id = $1 AND attr = $2;

-- name: MetricsBuildCounts :many
SELECT status, count(*) AS count FROM builds GROUP BY status;

-- name: MetricsAttributeCounts :many
SELECT status, count(*) AS count FROM build_attributes GROUP BY status;

-- name: MetricsQueueDepth :one
SELECT count(*) AS count FROM builds
WHERE status IN ('pending', 'evaluating', 'building');

-- name: MetricsBuildDuration :one
SELECT
    coalesce(sum(extract(epoch FROM finished_at - started_at)), 0)::float AS total,
    count(*) AS count
FROM builds
WHERE started_at IS NOT NULL AND finished_at IS NOT NULL;

-- name: MetricsProjects :one
SELECT count(*) FILTER (WHERE enabled) AS enabled, count(*) AS total
FROM projects;
