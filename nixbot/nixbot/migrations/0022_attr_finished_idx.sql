-- Cursor queries for build watchers: attributes finished since (finished_at, id).
CREATE INDEX build_attributes_build_finished_idx
    ON build_attributes (build_id, finished_at);
