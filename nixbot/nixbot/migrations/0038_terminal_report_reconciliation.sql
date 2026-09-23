-- A terminal build is pending reconciliation until every durable target has
-- accepted its current generation. Attaching a target clears this marker.
ALTER TABLE build_reporting
ADD COLUMN reported_generation BIGINT;
