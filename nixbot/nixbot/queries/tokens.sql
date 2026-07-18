-- API tokens, forge OAuth tokens and session revocation
-- (api_tokens.py, forge_tokens.py).

-- name: CreateApiToken :exec
INSERT INTO api_tokens (user_qualified, name, token_hash, expires_at, groups)
VALUES ($1, $2, $3, $4, $5);

-- name: ApiTokenByHash :one
-- Lazy pruning of expired rows piggybacks on every lookup; the
-- statement's snapshot may still return a just-pruned row, so the
-- caller re-checks expires_at.
WITH pruned AS (
    DELETE FROM api_tokens WHERE expires_at < now()
)
SELECT t.user_qualified, t.expires_at, t.groups
FROM api_tokens t WHERE t.token_hash = $1;

-- name: ApiTokensForUser :many
-- Same lazy pruning: keeps /settings free of long-expired tokens.
WITH pruned AS (
    DELETE FROM api_tokens WHERE expires_at < now()
)
SELECT t.id, t.name, t.created_at, t.expires_at FROM api_tokens t
WHERE t.user_qualified = $1 ORDER BY t.id;

-- name: RevokeApiToken :one
DELETE FROM api_tokens WHERE id = $1 AND user_qualified = $2 RETURNING id;

-- name: SaveForgeToken :exec
-- Lazy cleanup of expired rows piggybacks on every save. A row is
-- only useless once both the access token AND its refresh token (if
-- any) have expired.
WITH pruned AS (
    DELETE FROM forge_tokens
    WHERE GREATEST(expires_at, COALESCE(refresh_expires_at, expires_at)) < now()
)
INSERT INTO forge_tokens
    (session_id, token, expires_at, refresh_token, provider, refresh_expires_at)
VALUES (
    $1,
    $2,
    now() + make_interval(secs => sqlc.arg(lifetime)::float),
    sqlc.narg(refresh_token),
    sqlc.narg(provider),
    now() + make_interval(secs => sqlc.narg(refresh_lifetime)::float)
)
-- The CTE's deletes are invisible to the INSERT in the same
-- statement; an expired row under the same session id must be
-- overwritten, not collide.
ON CONFLICT (session_id) DO UPDATE
    SET token = EXCLUDED.token,
        expires_at = EXCLUDED.expires_at,
        refresh_token = EXCLUDED.refresh_token,
        provider = EXCLUDED.provider,
        refresh_expires_at = EXCLUDED.refresh_expires_at;

-- name: GetForgeToken :one
SELECT token FROM forge_tokens
WHERE session_id = $1 AND expires_at > now();

-- name: GetForgeRefreshToken :one
-- Refresh credentials for a session whose access token may already
-- be expired.
SELECT refresh_token, provider FROM forge_tokens
WHERE session_id = $1
  AND refresh_token IS NOT NULL
  AND provider IS NOT NULL
  AND COALESCE(refresh_expires_at, expires_at) > now();

-- name: DeleteForgeToken :exec
DELETE FROM forge_tokens WHERE session_id = $1;

-- name: RevokeSession :exec
-- Lazy pruning piggybacks on every revocation: rows are only needed
-- until the cookie itself would have expired.
WITH pruned AS (
    DELETE FROM revoked_sessions WHERE expires_at < now()
)
INSERT INTO revoked_sessions (session_id, expires_at)
VALUES ($1, now() + make_interval(secs => sqlc.arg(lifetime)::float))
-- The CTE's deletes are invisible to the INSERT in the same
-- statement; never let a conflicting stale row shorten the
-- revocation window.
ON CONFLICT (session_id) DO UPDATE
    SET expires_at = GREATEST(revoked_sessions.expires_at, EXCLUDED.expires_at);

-- name: SessionRevoked :one
SELECT EXISTS(SELECT 1 FROM revoked_sessions WHERE session_id = $1) AS revoked;
