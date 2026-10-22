BEGIN;
CREATE TABLE runtime.progress_snapshots (
  job_id uuid PRIMARY KEY REFERENCES runtime.jobs(job_id),
  version bigint NOT NULL DEFAULT 0,
  attempt_id uuid,
  sequence bigint NOT NULL DEFAULT 0,
  stages_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE runtime.progress_events (
  job_id uuid NOT NULL REFERENCES runtime.jobs(job_id),
  version bigint NOT NULL,
  event_id text NOT NULL,
  attempt_id uuid,
  payload_json jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (job_id, version),
  UNIQUE (job_id, event_id),
  CHECK (octet_length(payload_json::text) <= 8192)
);
-- Lifecycle records commit with the canonical change. Heartbeats do not emit events.
CREATE FUNCTION runtime.record_progress_lifecycle() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v bigint; jid uuid; kind text;
BEGIN
  jid := NEW.job_id;
  IF TG_TABLE_NAME = 'jobs' THEN
    IF TG_OP = 'UPDATE' THEN
      IF NEW.status IS NOT DISTINCT FROM OLD.status AND NEW.attempt_count = OLD.attempt_count THEN
        RETURN NEW;
      END IF;
    END IF;
    kind := NEW.status;
  ELSE
    IF NEW.status IS NOT DISTINCT FROM OLD.status THEN RETURN NEW; END IF;
    kind := 'attempt_' || NEW.status;
  END IF;
  INSERT INTO runtime.progress_snapshots(job_id, version) VALUES(jid, 1)
    ON CONFLICT(job_id) DO UPDATE SET version = runtime.progress_snapshots.version + 1,
      updated_at = clock_timestamp() RETURNING version INTO v;
  INSERT INTO runtime.progress_events(job_id,version,event_id,payload_json)
    VALUES(jid,v,'lifecycle:' || v,jsonb_build_object('kind','lifecycle','state',kind));
  RETURN NEW;
END $$;
CREATE TRIGGER jobs_progress AFTER INSERT OR UPDATE ON runtime.jobs
  FOR EACH ROW EXECUTE FUNCTION runtime.record_progress_lifecycle();
CREATE TRIGGER attempts_progress AFTER UPDATE OF status ON runtime.job_attempts
  FOR EACH ROW EXECUTE FUNCTION runtime.record_progress_lifecycle();
COMMIT;
