-- Wall time of the nix-eval-jobs run. Same lifetime as eval_completed.
ALTER TABLE builds ADD COLUMN eval_duration_ms BIGINT;
-- Per-attribute "stats" from nix-eval-jobs.
ALTER TABLE build_attributes
    ADD COLUMN eval_wall_ms INTEGER,
    ADD COLUMN eval_alloc_bytes BIGINT;
