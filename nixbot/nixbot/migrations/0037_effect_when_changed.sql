-- when.changed: normalised inputs of a run, the real run whose success a
-- later run reused, and restarts (which always run).
ALTER TABLE effect_runs
    ADD COLUMN changed_inputs JSONB,
    ADD COLUMN force_run BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN reused_from BIGINT REFERENCES effect_runs (id) ON DELETE SET NULL;
CREATE INDEX effect_runs_changed_idx ON effect_runs (project_id, name)
    WHERE changed_inputs IS NOT NULL AND status = 'succeeded'
      AND reused_from IS NULL;
