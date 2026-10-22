BEGIN;

CREATE TABLE IF NOT EXISTS runtime.source_receipts (
  receipt_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  source_system text NOT NULL CHECK (btrim(source_system) <> ''),
  source_key text NOT NULL CHECK (btrim(source_key) <> ''),

  producer_system text NOT NULL CHECK (btrim(producer_system) <> ''),
  workload_type text NOT NULL CHECK (btrim(workload_type) <> ''),

  route_id text NOT NULL CHECK (btrim(route_id) <> ''),
  route_kind text NOT NULL CHECK (btrim(route_kind) <> ''),
  channel_id text NULL CHECK (channel_id IS NULL OR btrim(channel_id) <> ''),
  matched_rule_version text NOT NULL CHECK (btrim(matched_rule_version) <> ''),

  source_json jsonb NOT NULL DEFAULT '{}'::jsonb,

  status text NOT NULL CHECK (status IN ('accepted', 'submitted', 'cancelled')),
  job_id uuid NULL,

  created_at timestamptz NOT NULL DEFAULT NOW(),
  updated_at timestamptz NOT NULL DEFAULT NOW(),

  CHECK (
    (status = 'submitted' AND job_id IS NOT NULL)
    OR
    (status IN ('accepted', 'cancelled') AND job_id IS NULL)
  )
);

COMMENT ON TABLE runtime.source_receipts IS 'Durable source admission receipts. Producers resolve routes before inserting; runtime stores dedupe and provenance only.';
COMMENT ON COLUMN runtime.source_receipts.source_key IS 'Producer-provided source idempotency key, e.g. tg:<bot_identity>:<chat_id>:<message_id>.';
COMMENT ON COLUMN runtime.source_receipts.route_id IS 'Producer route registry id that resolved this source update.';
COMMENT ON COLUMN runtime.source_receipts.route_kind IS 'Producer route kind such as pi_prompt_route, rss_publish_route, meeting_note_route, or transcribe_audio_route.';
COMMENT ON COLUMN runtime.source_receipts.matched_rule_version IS 'Version of the cheap deterministic route rule that admitted this update.';

ALTER TABLE runtime.source_receipts
  ADD CONSTRAINT source_receipts_source_key_uidx UNIQUE (source_system, source_key);

ALTER TABLE runtime.source_receipts
  ADD CONSTRAINT source_receipts_job_id_fkey
  FOREIGN KEY (job_id)
  REFERENCES runtime.jobs (job_id)
  ON DELETE RESTRICT;

CREATE UNIQUE INDEX IF NOT EXISTS source_receipts_job_id_uidx
  ON runtime.source_receipts (job_id)
  WHERE job_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS source_receipts_route_idx
  ON runtime.source_receipts (route_kind, route_id, created_at DESC, receipt_id);

CREATE INDEX IF NOT EXISTS source_receipts_producer_status_idx
  ON runtime.source_receipts (producer_system, status, created_at DESC, receipt_id);

CREATE INDEX IF NOT EXISTS source_receipts_channel_idx
  ON runtime.source_receipts (channel_id, created_at DESC, receipt_id)
  WHERE channel_id IS NOT NULL;

CREATE OR REPLACE FUNCTION runtime.enforce_source_receipt_status_transitions()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'accepted' THEN
      RAISE EXCEPTION 'runtime.source_receipts rows must be inserted in accepted state';
    END IF;
    RETURN NEW;
  END IF;

  IF NEW.status = OLD.status THEN
    RETURN NEW;
  END IF;

  IF (
    (OLD.status = 'accepted' AND NEW.status IN ('submitted', 'cancelled'))
  ) THEN
    RETURN NEW;
  END IF;

  RAISE EXCEPTION 'invalid runtime.source_receipts status transition: % -> % for receipt %', OLD.status, NEW.status, OLD.receipt_id;
END;
$$;

DROP TRIGGER IF EXISTS source_receipts_status_transition_trg ON runtime.source_receipts;
CREATE TRIGGER source_receipts_status_transition_trg
BEFORE INSERT OR UPDATE ON runtime.source_receipts
FOR EACH ROW
EXECUTE FUNCTION runtime.enforce_source_receipt_status_transitions();

DROP TRIGGER IF EXISTS source_receipts_set_updated_at ON runtime.source_receipts;
CREATE TRIGGER source_receipts_set_updated_at
BEFORE UPDATE ON runtime.source_receipts
FOR EACH ROW
EXECUTE FUNCTION runtime.set_updated_at();

COMMIT;
