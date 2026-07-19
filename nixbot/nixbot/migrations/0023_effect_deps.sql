-- Effects can declare dependencies on other effects of the same build
-- (`after` in the flake). The claim-time check needs them persisted:
-- the queue item only carries (build_id, name).
ALTER TABLE build_effects ADD COLUMN deps JSONB;
