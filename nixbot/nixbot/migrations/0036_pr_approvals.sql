-- PRs from untrusted authors awaiting maintainer approval. Approval
-- unlocks the whole PR. `pending` is the newest gated ChangeRequest.
CREATE TABLE pr_approvals (
    project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    pr_number BIGINT NOT NULL,
    pending JSONB,
    approved_by TEXT,
    approved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, pr_number)
);
