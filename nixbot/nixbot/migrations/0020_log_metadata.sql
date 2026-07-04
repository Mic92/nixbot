-- Log storage refactor: a log's path is fully determined by
-- (build_id, attr/effect name), so storing it invited drift between the
-- DB row and the file on disk (stale logs after a restart, orphaned
-- files). Keep only the display metadata (size/truncated), inline on
-- the owning row like build_effects already does, and compute the path
-- from the name everywhere else.

ALTER TABLE build_attributes
    ADD COLUMN log_size BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN log_truncated BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE build_attributes a
SET log_size = l.size_bytes, log_truncated = l.truncated
FROM logs l WHERE l.attribute_id = a.id;

DROP TABLE logs;

ALTER TABLE build_effects DROP COLUMN log_path;
