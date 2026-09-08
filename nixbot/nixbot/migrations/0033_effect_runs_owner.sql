-- Migration 0028 originally created effect_runs without owner. Adding the
-- generated column to that migration only covered fresh databases, leaving
-- upgraded installations incompatible with queries that use owner.
ALTER TABLE effect_runs
ADD COLUMN IF NOT EXISTS owner TEXT GENERATED ALWAYS AS (
    CASE
        WHEN kind IN ('push', 'check') THEN 'build'
        WHEN kind = 'schedule' THEN 'schedule'
        ELSE 'delivery'
    END
) STORED;
