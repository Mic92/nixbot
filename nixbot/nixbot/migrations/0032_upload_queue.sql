-- Store paths waiting for a binary-cache push (upload.py). Persisted so
-- a restart does not lose intermediates built since the last batch.
CREATE TABLE upload_queue (
    id BIGSERIAL PRIMARY KEY,
    uploader TEXT NOT NULL,
    path TEXT NOT NULL,
    attempts INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX upload_queue_uploader_idx ON upload_queue (uploader, id);
