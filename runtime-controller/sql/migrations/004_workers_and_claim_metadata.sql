BEGIN;

CREATE TABLE IF NOT EXISTS runtime.workers (
  worker_id text PRIMARY KEY CHECK (btrim(worker_id) <> ''),
  venue text NOT NULL CHECK (venue IN ('generic', 'oci', 'modal')),
  state text NOT NULL CHECK (state IN ('active', 'draining', 'drained')) DEFAULT 'active',

  supported_execution_profiles text[] NOT NULL DEFAULT '{}'::text[],
  supported_workload_types text[] NOT NULL DEFAULT '{}'::text[],
  allowed_producer_systems text[] NOT NULL DEFAULT '{}'::text[],
  allowed_queue_classes text[] NOT NULL DEFAULT '{}'::text[],
  queue_preference text[] NOT NULL DEFAULT '{}'::text[],

  capacity integer NOT NULL DEFAULT 1 CHECK (capacity >= 0),
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,

  last_heartbeat_at timestamptz NOT NULL DEFAULT NOW(),
  last_claim_at timestamptz NULL,

  drain_requested_at timestamptz NULL,
  drain_deadline_at timestamptz NULL,
  drain_reason text NULL,

  created_at timestamptz NOT NULL DEFAULT NOW(),
  updated_at timestamptz NOT NULL DEFAULT NOW(),

  CHECK (
    (state = 'draining' AND drain_requested_at IS NOT NULL)
    OR state <> 'draining'
  )
);

CREATE INDEX IF NOT EXISTS workers_state_venue_idx
  ON runtime.workers (state, venue, worker_id);

CREATE INDEX IF NOT EXISTS workers_last_heartbeat_idx
  ON runtime.workers (last_heartbeat_at DESC, worker_id);

DROP TRIGGER IF EXISTS workers_set_updated_at ON runtime.workers;
CREATE TRIGGER workers_set_updated_at
BEFORE UPDATE ON runtime.workers
FOR EACH ROW EXECUTE FUNCTION runtime.set_updated_at();

ALTER TABLE runtime.job_attempts
  ADD COLUMN IF NOT EXISTS claim_json jsonb NULL;

COMMIT;
