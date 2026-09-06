-- Stable, unguessable public badge URLs. UUIDs carry enough entropy to
-- serve as bearer capabilities: knowing one exposes only that project's
-- rendered build status, while repository discovery still requires auth.

ALTER TABLE projects
ADD COLUMN badge_token UUID NOT NULL DEFAULT gen_random_uuid();

CREATE UNIQUE INDEX projects_badge_token_idx ON projects (badge_token);
