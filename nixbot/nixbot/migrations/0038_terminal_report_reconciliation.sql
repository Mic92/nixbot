-- A terminal build is pending reconciliation until every durable target has
-- accepted its current generation. Attaching a target clears this marker.
ALTER TABLE build_reporting
ADD COLUMN reported_generation BIGINT;

-- Do not replay all historical terminal builds when the acknowledgment state
-- is introduced. New terminalizations and later target attachments invalidate
-- the marker through their normal lifecycle paths.
UPDATE build_reporting r
SET reported_generation = b.status_generation
FROM builds b
WHERE b.id = r.build_id
  AND b.status IN ('succeeded', 'failed', 'cancelled');
