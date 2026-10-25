-- Builds of pull requests from outside contributors. Set when the build
-- is created and never changed: an approval lets the pull request build,
-- it does not make its code ours.
ALTER TABLE builds ADD COLUMN external BOOLEAN NOT NULL DEFAULT FALSE;
