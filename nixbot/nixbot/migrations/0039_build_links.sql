-- Commits that reused a build of the same tree. A restart reports to
-- these as well as to the build's own commit
CREATE TABLE build_links (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    build_id    BIGINT NOT NULL REFERENCES builds (id) ON DELETE CASCADE,
    branch      TEXT   NOT NULL,
    commit_sha  TEXT   NOT NULL,
    pr_number   BIGINT,
    tag         TEXT,
    actor       TEXT
);
CREATE UNIQUE INDEX build_links_context_idx ON build_links (
    build_id, commit_sha, branch, COALESCE(pr_number, 0), COALESCE(tag, '')
);
