-- Project store and webhook secrets (repos.py, hook_secrets.py,
-- visibility.py, web/control_routes.py).

-- name: CountDiscoveredProjects :one
SELECT count(*) AS count FROM projects WHERE forge <> 'pull_based';

-- name: UpsertDiscoveredProjects :exec
-- Batch upsert of one discovery cycle; arrays are zipped
-- positionally by unnest.
INSERT INTO projects
    (forge, forge_repo_id, owner, name, default_branch,
     url, private, enabled)
SELECT u.forge, u.forge_repo_id, u.owner, u.name, u.default_branch,
       u.url, u.private, u.enabled
FROM (SELECT unnest(sqlc.arg(forges)::text[]) AS forge,
             unnest(sqlc.arg(forge_repo_ids)::text[]) AS forge_repo_id,
             unnest(sqlc.arg(owners)::text[]) AS owner,
             unnest(sqlc.arg(names)::text[]) AS name,
             unnest(sqlc.arg(default_branches)::text[]) AS default_branch,
             unnest(sqlc.arg(urls)::text[]) AS url,
             unnest(sqlc.arg(privates)::boolean[]) AS private,
             unnest(sqlc.arg(enableds)::boolean[]) AS enabled) u
ON CONFLICT (forge, forge_repo_id) DO UPDATE SET
    owner = EXCLUDED.owner,
    name = EXCLUDED.name,
    default_branch = EXCLUDED.default_branch,
    url = EXCLUDED.url,
    private = EXCLUDED.private,
    updated_at = now()
-- Discovery re-upserts every repo every poll cycle; skip no-op
-- updates to avoid WAL/autovacuum churn.
WHERE (projects.owner, projects.name,
       projects.default_branch, projects.url,
       projects.private)
      IS DISTINCT FROM
      (EXCLUDED.owner, EXCLUDED.name,
       EXCLUDED.default_branch, EXCLUDED.url,
       EXCLUDED.private);

-- name: CountEnabledProjects :one
SELECT count(*) AS count FROM projects WHERE enabled;

-- name: UpsertPullBasedProjects :exec
-- Batch upsert of the statically configured pull-based repos; arrays
-- are zipped positionally by unnest.
INSERT INTO projects
    (forge, forge_repo_id, owner, name, default_branch,
     url, private, enabled)
SELECT 'pull_based', u.forge_repo_id, u.owner, u.name, u.default_branch,
       u.url, FALSE, TRUE
FROM (SELECT unnest(sqlc.arg(forge_repo_ids)::text[]) AS forge_repo_id,
             unnest(sqlc.arg(owners)::text[]) AS owner,
             unnest(sqlc.arg(names)::text[]) AS name,
             unnest(sqlc.arg(default_branches)::text[]) AS default_branch,
             unnest(sqlc.arg(urls)::text[]) AS url) u
ON CONFLICT (forge, forge_repo_id) DO UPDATE SET
    default_branch = EXCLUDED.default_branch,
    url = EXCLUDED.url,
    updated_at = now()
-- Same no-op skip as UpsertDiscoveredProject: this runs on every
-- reconcile tick for every configured repo.
WHERE (projects.default_branch, projects.url)
      IS DISTINCT FROM
      (EXCLUDED.default_branch, EXCLUDED.url);

-- name: SetProjectEnabled :exec
UPDATE projects SET enabled = $2, updated_at = now() WHERE id = $1;

-- name: PruneMissingDisabledProjects :exec
-- Drop disabled repos of one forge that discovery no longer returned.
-- Enabled projects are kept so build history survives transient
-- visibility loss.
DELETE FROM projects
WHERE forge = $1
  AND NOT enabled
  AND forge_repo_id <> ALL (sqlc.arg(forge_repo_ids)::text[]);

-- name: ToggleProjectEnabled :exec
UPDATE projects SET enabled = NOT enabled, updated_at = now() WHERE id = $1;

-- name: EnabledProjects :many
SELECT * FROM projects WHERE enabled ORDER BY owner, name;

-- name: ProjectById :one
SELECT * FROM projects WHERE id = $1;

-- name: ReconcileWatermark :one
SELECT reconcile_watermark FROM projects WHERE id = $1;

-- name: AdvanceReconcileWatermark :exec
-- Advance (never rewind) the reconcile watermark.
UPDATE projects SET reconcile_watermark = $2 WHERE id = $1
AND (reconcile_watermark IS NULL OR reconcile_watermark < $2);

-- name: ProjectByForgeId :one
SELECT * FROM projects WHERE forge = $1 AND forge_repo_id = $2;

-- name: ProjectVisibilityRows :many
SELECT id, forge, forge_repo_id, owner, name, private FROM projects;

-- name: ProjectForgeIds :many
SELECT id, forge, forge_repo_id FROM projects;

-- name: WebhookSecretByForgeRepo :one
SELECT s.secret FROM webhook_secrets s
JOIN projects p ON p.id = s.project_id
WHERE p.forge = $1 AND p.forge_repo_id = $2;

-- name: WebhookSecret :one
SELECT secret FROM webhook_secrets WHERE project_id = $1;

-- name: CreateWebhookSecret :one
-- Concurrent creation: first writer wins.
INSERT INTO webhook_secrets (project_id, secret)
VALUES ($1, $2)
ON CONFLICT (project_id) DO UPDATE SET secret = webhook_secrets.secret
RETURNING secret;

-- name: RotateWebhookSecret :one
INSERT INTO webhook_secrets (project_id, secret)
VALUES ($1, $2)
ON CONFLICT (project_id) DO UPDATE SET secret = EXCLUDED.secret
RETURNING secret;
