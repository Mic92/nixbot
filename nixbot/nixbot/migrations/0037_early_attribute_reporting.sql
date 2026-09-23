-- Durable state for prompt per-attribute failure reporting.
CREATE TABLE build_reporting (
    build_id BIGINT PRIMARY KEY REFERENCES builds (id) ON DELETE CASCADE,
    attribute_prefix TEXT NOT NULL DEFAULT 'checks'
);

-- A tree-identical build can serve several commits (PR head and main).
CREATE TABLE build_report_targets (
    build_id BIGINT NOT NULL REFERENCES builds (id) ON DELETE CASCADE,
    commit_sha TEXT NOT NULL,
    branch TEXT NOT NULL,
    pr_number BIGINT,
    PRIMARY KEY (build_id, commit_sha)
);

INSERT INTO build_report_targets (build_id, commit_sha, branch, pr_number)
SELECT id, commit_sha, branch, pr_number FROM builds;

-- Reservations survive success flips, sharing the configured report budget
-- across early reports, retries, and final reconciliation.
CREATE TABLE failure_report_reservations (
    revision TEXT NOT NULL,
    status_name TEXT NOT NULL,
    timestamp DOUBLE PRECISION NOT NULL,
    delivered_attempt TEXT,
    PRIMARY KEY (revision, status_name)
);
