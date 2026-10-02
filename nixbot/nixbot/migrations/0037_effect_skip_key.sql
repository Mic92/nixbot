-- An onPush effect may declare a skipKey naming what it does. A later
-- run with the key of one that succeeded is recorded as succeeded
-- without running (effects_run.py).
ALTER TABLE effect_runs ADD COLUMN skip_key TEXT;
CREATE INDEX effect_runs_skip_key_idx ON effect_runs (project_id, name, skip_key)
    WHERE skip_key IS NOT NULL AND status = 'succeeded';
