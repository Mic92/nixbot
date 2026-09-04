-- onEvent deliveries (#165) are effect_runs rows attached to the build
-- the event is about, with kind = the event kind. payload is what the
-- effect sees as /run/event.json, code_rev the default-branch commit
-- its definition came from, skip_reason why `when` did not match, lock
-- the expanded `lock` so a UI restart re-enqueues under the same
-- work-queue key deliveries use.
ALTER TABLE effect_runs
    ADD COLUMN payload JSONB,
    ADD COLUMN code_rev TEXT,
    ADD COLUMN skip_reason TEXT,
    ADD COLUMN actor TEXT,
    ADD COLUMN lock TEXT;

-- Who caused the build (webhook sender or restarting user), forge
-- qualified like pr_author. NULL for polled changes.
ALTER TABLE builds ADD COLUMN actor TEXT;

-- onPush/onEvent that failed to evaluate. source 'onPush'/'onEvent':
-- the built commit. 'delivery': the default branch at code_rev.
CREATE TABLE effect_eval_errors (
    build_id BIGINT NOT NULL REFERENCES builds (id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    error TEXT NOT NULL,
    code_rev TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (build_id, source)
);
