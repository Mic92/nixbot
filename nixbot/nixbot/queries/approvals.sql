-- name: PrApproved :one
SELECT 1 AS approved FROM pr_approvals
WHERE project_id = $1 AND pr_number = $2 AND approved_at IS NOT NULL;

-- name: GatePr :exec
INSERT INTO pr_approvals (project_id, pr_number, pending)
VALUES ($1, $2, $3)
ON CONFLICT (project_id, pr_number) DO UPDATE
SET pending = EXCLUDED.pending, created_at = now();

-- name: ApprovePr :one
WITH held AS (
    SELECT h.pending FROM pr_approvals h
    WHERE h.project_id = $1 AND h.pr_number = $2 FOR UPDATE
), up AS (
    INSERT INTO pr_approvals (project_id, pr_number, approved_by, approved_at)
    VALUES ($1, $2, sqlc.narg(approved_by)::text, now())
    ON CONFLICT (project_id, pr_number) DO UPDATE
    SET approved_by = EXCLUDED.approved_by,
        approved_at = COALESCE(pr_approvals.approved_at, EXCLUDED.approved_at),
        pending = NULL
)
SELECT pending FROM held;

-- name: PendingApprovals :many
SELECT pr_number,
       (pending->>'pr_author')::text AS pr_author,
       (pending->>'commit_sha')::text AS commit_sha,
       created_at
FROM pr_approvals
WHERE project_id = $1 AND approved_at IS NULL AND pending IS NOT NULL
ORDER BY created_at DESC;

-- name: DropPendingApproval :exec
DELETE FROM pr_approvals
WHERE project_id = $1 AND pr_number = $2 AND approved_at IS NULL;
