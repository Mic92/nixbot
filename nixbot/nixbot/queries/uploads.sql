-- Binary-cache upload queue (upload.py).

-- name: EnqueueUploadPaths :many
INSERT INTO upload_queue (uploader, path)
SELECT sqlc.arg(uploader)::text, unnest(sqlc.arg(paths)::text[])
RETURNING id;

-- name: PendingUploadPaths :many
SELECT id, path, attempts FROM upload_queue
WHERE uploader = $1
ORDER BY id
LIMIT $2;

-- name: DeleteUploadPaths :exec
DELETE FROM upload_queue WHERE id = ANY(sqlc.arg(ids)::bigint[]);

-- name: RetryUploadPaths :exec
-- One unpushable path must not wedge the uploader forever.
WITH dropped AS (
    DELETE FROM upload_queue
    WHERE id = ANY(sqlc.arg(ids)::bigint[])
      AND attempts + 1 >= sqlc.arg(max_attempts)::int
)
UPDATE upload_queue SET attempts = attempts + 1
WHERE id = ANY(sqlc.arg(ids)::bigint[])
  AND attempts + 1 < sqlc.arg(max_attempts)::int;

-- name: DropUnknownUploaders :exec
DELETE FROM upload_queue WHERE NOT (uploader = ANY(sqlc.arg(names)::text[]));
