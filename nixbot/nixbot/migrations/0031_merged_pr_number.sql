-- A merge push reuses the PR build for the default branch and detaches
-- it from the PR. Remember the PR so its close/merge event still finds
-- the build for onEvent.pull_request_closed.
ALTER TABLE builds ADD COLUMN merged_pr_number BIGINT;
