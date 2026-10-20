-- Work queue (work_queue.py).

-- name: EnqueueWorkItem :one
INSERT INTO work_queue (kind, dedup_key, payload, not_before)
VALUES ($1, $2, sqlc.arg(payload)::jsonb,
        CASE WHEN sqlc.arg(delay)::double precision > 0
             THEN now() + make_interval(secs => sqlc.arg(delay)::double precision) END)
ON CONFLICT (kind, dedup_key, md5(payload::text))
WHERE status = 'pending'
DO NOTHING
RETURNING id;

-- name: EnqueueEffectItems :exec
-- One queue item per effect. dedup_keys parallels names: a per-effect
-- key, or a shared lock key for effects that declared a lock.
-- The jsonb payloads are built server-side so their canonical text
-- form matches what the dedup index hashes.
INSERT INTO work_queue (kind, dedup_key, payload)
SELECT 'effect', u.dedup_key,
       jsonb_build_object('build_id', sqlc.arg(build_id)::bigint,
                          'kind', sqlc.arg(kind)::text,
                          'name', u.name)
FROM (SELECT unnest(sqlc.arg(names)::text[]) AS name,
             unnest(sqlc.arg(dedup_keys)::text[]) AS dedup_key) AS u
-- Fixed order avoids deadlocks between concurrent callers.
ORDER BY u.name
ON CONFLICT (kind, dedup_key, md5(payload::text))
WHERE status = 'pending'
DO NOTHING;

-- name: ClaimNextWorkItem :one
-- Items sharing a dedup key are strict FIFO: one running at a time,
-- nothing overtakes an older pending item. For effect locks that means
-- a build's locked effects all finish before the next build's start.
-- Effect items are additionally held back until every declared
-- dependency settled; the runner then decides between run and skip.
-- A pull request based on another one's head branch waits for that
-- PR's queued or running change items, whatever their outcome: its
-- merge with the base contains the base PR's tree, so the store then
-- holds most of what it needs. Fork heads and the default branch never
-- hold, and neither does a cycle of PRs based on each other.
WITH RECURSIVE pulls AS (
    SELECT id, payload->>'forge' AS forge, payload->>'forge_repo_id' AS repo,
           payload->>'branch' AS base, payload->>'head_ref' AS head,
           (payload->>'head_in_base_repo')::boolean AS in_repo
    FROM work_queue
    WHERE kind = 'change' AND status IN ('pending', 'running')
      AND payload->>'pr_number' IS NOT NULL
),
-- Each PR's base branch, and the bases of the PRs below it.
below (id, forge, repo, branch) AS (
    SELECT id, forge, repo, base FROM pulls
    UNION
    SELECT below.id, below.forge, below.repo, p.base FROM below
    JOIN pulls p ON p.forge = below.forge AND p.repo = below.repo
        AND p.in_repo AND p.head = below.branch
),
held AS (
    SELECT p.id FROM pulls p
    WHERE EXISTS (
        SELECT 1 FROM pulls b
        WHERE b.forge = p.forge AND b.repo = p.repo
          AND b.in_repo AND b.head = p.base
      )
      AND NOT EXISTS (
        SELECT 1 FROM projects j
        WHERE j.forge = p.forge AND j.forge_repo_id = p.repo
          AND j.default_branch = p.base
      )
      AND NOT EXISTS (
        SELECT 1 FROM below WHERE below.id = p.id AND below.branch = p.head
      )
)
UPDATE work_queue SET status = 'running', claimed_at = now()
WHERE id = (
    SELECT id FROM work_queue w
    WHERE w.status = 'pending'
      AND (w.not_before IS NULL OR w.not_before <= now())
      AND NOT EXISTS (
        SELECT 1 FROM work_queue r
        WHERE r.dedup_key = w.dedup_key
          AND (r.status = 'running'
               OR (r.status = 'pending'
                   AND (r.created_at, r.id) < (w.created_at, w.id)))
      )
      AND NOT (w.kind = 'effect' AND EXISTS (
        SELECT 1 FROM effect_runs e
        JOIN effect_runs d ON d.build_id = e.build_id AND d.kind = 'push'
            AND d.name IN (SELECT jsonb_array_elements_text(e.deps))
        WHERE e.build_id = (w.payload->>'build_id')::bigint
          AND e.kind = 'push' AND w.payload->>'kind' = 'push'
          AND e.name = w.payload->>'name'
          AND d.status IN ('pending', 'running')
      ))
      AND w.id NOT IN (SELECT id FROM held)
    ORDER BY w.created_at, w.id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING id, kind, dedup_key, payload, attempts;

-- name: FinishWorkItem :exec
UPDATE work_queue
SET status = $2, error = sqlc.narg(error), finished_at = now()
WHERE id = $1;

-- name: RetryWorkItem :execrows
-- Back to pending after a transient failure, keeping its place in the
-- per-key FIFO. No row when an identical pending item exists already.
UPDATE work_queue w SET status = 'pending', claimed_at = NULL,
    attempts = w.attempts + 1, error = sqlc.arg(error)::text,
    not_before = now() + make_interval(secs => sqlc.arg(delay)::double precision)
WHERE w.id = sqlc.arg(id)::bigint AND NOT EXISTS (
    SELECT 1 FROM work_queue p
    WHERE p.kind = w.kind AND p.dedup_key = w.dedup_key
      AND md5(p.payload::text) = md5(w.payload::text)
      AND p.status = 'pending' AND p.id <> w.id
);

-- name: SettleInterruptedWork :exec
-- Startup sweep over rows the previous process died holding: requeue
-- them, except where an identical intent was re-enqueued before the
-- crash (the pending row carries it; requeueing would hit
-- pending_uniq). Both branches read the same statement snapshot with
-- complementary predicates, so every interrupted row takes exactly
-- one of them, atomically.
WITH requeued AS (
    UPDATE work_queue w SET status = 'pending', claimed_at = NULL
    WHERE w.status = 'running' AND NOT EXISTS (
        SELECT 1 FROM work_queue p
        WHERE p.kind = w.kind AND p.dedup_key = w.dedup_key
          AND p.status = 'pending'
    )
)
UPDATE work_queue w SET status = 'failed', finished_at = now(),
    error = 'interrupted; superseded by a newer request'
WHERE w.status = 'running' AND EXISTS (
    SELECT 1 FROM work_queue p
    WHERE p.kind = w.kind AND p.dedup_key = w.dedup_key
      AND p.status = 'pending'
);

-- name: CleanupWorkQueue :exec
DELETE FROM work_queue
WHERE finished_at IS NOT NULL
  AND finished_at < now() - make_interval(days => sqlc.arg(retention_days)::int);
