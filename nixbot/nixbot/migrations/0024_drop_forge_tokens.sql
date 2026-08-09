-- Repo access is checked with the service's own forge credentials, so
-- user OAuth tokens are no longer stored.
DROP TABLE IF EXISTS forge_tokens;
