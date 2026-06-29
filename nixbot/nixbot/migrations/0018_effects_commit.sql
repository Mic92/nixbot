-- The commit that triggers an effects run can differ from the build's
-- stored commit_sha: a default-branch push routinely reuses a PR build
-- whose commit_sha is the PR head. Effects then run on behalf of the
-- default-branch commit, so their statuses must land there, not on the
-- PR head. Record the triggering ref when effects start; effect items
-- only carry build_id and would otherwise fall back to commit_sha.
ALTER TABLE builds ADD COLUMN effects_commit_sha TEXT;
ALTER TABLE builds ADD COLUMN effects_branch TEXT;
ALTER TABLE builds ADD COLUMN effects_pr_number BIGINT;
