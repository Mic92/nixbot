-- One table for every effect run. build_effects (onPush, owned by a
-- build) and scheduled_effect_runs (onSchedule, owned by a project) had
-- grown parallel log/web/recovery stacks. Upcoming onEvent effects would
-- have needed a third. `kind` says what triggered the run. build-owned
-- rows stay one per (build, name) and are reset in place on restart.
-- Rows without a build (schedules) are history.
CREATE TABLE effect_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    -- 'push' | 'check' | 'schedule' | an onEvent kind
    kind TEXT NOT NULL,
    -- Which lifecycle the row follows: the build, event deliveries, none.
    owner TEXT NOT NULL GENERATED ALWAYS AS (
        CASE
            WHEN kind IN ('push', 'check') THEN 'build'
            WHEN kind = 'schedule' THEN 'schedule'
            ELSE 'delivery'
        END
    ) STORED,
    build_id BIGINT REFERENCES builds (id) ON DELETE CASCADE,
    schedule_name TEXT,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    error TEXT,
    deps JSONB,
    log_size BIGINT NOT NULL DEFAULT 0,
    log_truncated BOOLEAN NOT NULL DEFAULT FALSE,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    -- NULL build_id never conflicts, so schedule rows accumulate.
    UNIQUE (build_id, kind, name),
    CHECK ((kind = 'schedule') = (schedule_name IS NOT NULL)),
    CHECK (kind = 'schedule' OR build_id IS NOT NULL)
);
CREATE INDEX effect_runs_project_idx ON effect_runs (project_id, started_at DESC);

INSERT INTO effect_runs (project_id, kind, build_id, name, status, error, deps,
                         log_size, log_truncated, started_at, finished_at)
SELECT b.project_id, 'push', e.build_id, e.name, e.status, e.error, e.deps,
       e.log_size, e.log_truncated, e.started_at, e.finished_at
FROM build_effects e JOIN builds b ON b.id = e.build_id
ORDER BY e.id;

INSERT INTO effect_runs (project_id, kind, schedule_name, name, status, error,
                         started_at, finished_at)
SELECT project_id, 'schedule', schedule_name, effect, status, error,
       started_at, finished_at
FROM scheduled_effect_runs
ORDER BY id;

DROP TABLE build_effects;
DROP TABLE scheduled_effect_runs;
DROP FUNCTION notify_effect_status();
DROP FUNCTION notify_scheduled_run_status();

-- One notify shape for all kinds. build_id is null for schedule runs,
-- which is how listeners told the two apart before. Names are
-- repo-controlled, so truncate them (cf. migration 0012).
CREATE FUNCTION notify_effect_run_status() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.status IS NOT DISTINCT FROM NEW.status THEN
        RETURN NEW;
    END IF;
    PERFORM pg_notify('build_events', json_strip_nulls(json_build_object(
        'project_id', NEW.project_id,
        'run_id', NEW.id,
        'build_id', NEW.build_id,
        'schedule_name', left(NEW.schedule_name, 256),
        'effect', left(NEW.name, 256),
        'status', NEW.status))::text);
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER effect_runs_status_notify
AFTER INSERT OR UPDATE ON effect_runs
FOR EACH ROW EXECUTE FUNCTION notify_effect_run_status();
