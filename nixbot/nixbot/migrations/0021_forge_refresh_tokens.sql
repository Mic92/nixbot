-- Gitea/OIDC access tokens expire after ~1h while the session cookie
-- lives ~30 days; store the OAuth refresh token (plus which login
-- provider issued it) so an expired access token can be renewed.
ALTER TABLE forge_tokens
    ADD COLUMN refresh_token TEXT,
    ADD COLUMN provider TEXT,
    -- How long the refresh token itself may be used (capped at the
    -- session lifetime); expires_at keeps meaning "access token expiry".
    ADD COLUMN refresh_expires_at TIMESTAMPTZ;
