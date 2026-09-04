-- Work queue (work_queue.py).

-- name: EnqueueWorkItem :one
INSERT INTO work_queue (kind, dedup_key, payload)
VALUES ($1, $2, sqlc.arg(payload)::jsonb)
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
ON CONFLICT (kind, dedup_key, md5(payload::text))
WHERE status = 'pending'
DO NOTHING;

-- name: ClaimNextWorkItem :one
-- Items sharing a dedup key are strict FIFO: one running at a time,
-- nothing overtakes an older pending item. For effect locks that means
-- a build's locked effects all finish before the next build's start.
-- Effect items are additionally held back until every declared
-- dependency settled; the runner then decides between run and skip.
UPDATE work_queue SET status = 'running', claimed_at = now()
WHERE id = (
    SELECT id FROM work_queue w
    WHERE w.status = 'pending'
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
    ORDER BY w.created_at, w.id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING id, kind, dedup_key, payload;

-- name: FinishWorkItem :exec
UPDATE work_queue
SET status = $2, error = sqlc.narg(error), finished_at = now()
WHERE id = $1;

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
