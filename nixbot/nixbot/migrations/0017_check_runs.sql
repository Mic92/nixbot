-- GitHub check-run ids per (project, sha, check name).
--
-- Keyed by sha, NOT build_id: GitHub does not dedupe check runs by
-- name, and the same sha routinely has >=2 builds here (branch push +
-- PR on the same commit, build reuse). Keying by build would stack
-- duplicate nixbot/nix-eval runs in the merge box - a regression vs
-- commit status, which dedupes by (sha, context).
--
-- attr is NULL for the nix-eval / nix-build summary runs and carries
-- the attribute name for per-attr runs so a check_run rerequested
-- webhook maps straight to restart_attribute().
CREATE TABLE check_runs (
    project_id  BIGINT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    sha         TEXT   NOT NULL,
    name        TEXT   NOT NULL,
    attr        TEXT,
    external_id BIGINT NOT NULL,
    -- Retention only; rows can outlive any single build on the sha.
    timestamp   DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (project_id, sha, name)
);

-- check_suite rerequested only carries head_sha; resolve it to the
-- newest build on that commit.
CREATE INDEX builds_project_commit_sha_idx ON builds (project_id, commit_sha);
