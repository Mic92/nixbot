-- 'skipped' now means gated off on this ref (e.g. pull requests).
-- Effects that did not run because a dependency failed get the same
-- name attributes use.
UPDATE build_effects SET status = 'dependency_failed' WHERE status = 'skipped';
