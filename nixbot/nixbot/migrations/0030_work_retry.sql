-- Transient failures (forge 5xx, rate limits) put the item back to
-- pending with a delay instead of failing it.
ALTER TABLE work_queue
    ADD COLUMN attempts INT NOT NULL DEFAULT 0,
    ADD COLUMN not_before TIMESTAMPTZ;
