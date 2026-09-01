-- onEvent deliveries (#165) are effect_runs rows attached to the build
-- the event is about, with kind = the event kind. payload is what the
-- effect sees as /run/event.json, code_rev the default-branch commit
-- its definition came from, skip_reason why `when` did not match.
ALTER TABLE effect_runs
    ADD COLUMN payload JSONB,
    ADD COLUMN code_rev TEXT,
    ADD COLUMN skip_reason TEXT,
    ADD COLUMN actor TEXT;

-- Who caused the build (webhook sender or restarting user), forge
-- qualified like pr_author. NULL for polled changes.
ALTER TABLE builds ADD COLUMN actor TEXT;
