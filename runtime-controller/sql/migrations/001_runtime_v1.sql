-- Runtime controller v1 DDL
-- Source packet:
--   <design-archive>/sql_runtime-controller-v1-ddl_2026-04-11c.sql
--
-- Scope:
-- - generic bounded-work runtime substrate
-- - explicit terminal result handoff
-- - fenced lease attempts
-- - narrow artifact registry
-- - no runtime-side queued-job coalescing

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS runtime;

CREATE OR REPLACE FUNCTION runtime.set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at := NOW();
  RETURN NEW;
END;
$$;

CREATE TABLE IF NOT EXISTS runtime.jobs (
  job_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  producer_system text NOT NULL CHECK (btrim(producer_system) <> ''),
  producer_ref text NOT NULL CHECK (btrim(producer_ref) <> ''),
  result_owner text NOT NULL CHECK (btrim(result_owner) <> ''),
  result_route text NOT NULL CHECK (btrim(result_route) <> ''),
  workload_type text NOT NULL CHECK (btrim(workload_type) <> ''),

  queue_class text NOT NULL CHECK (queue_class IN ('foreground', 'background', 'ops')),
  execution_profile text NOT NULL CHECK (btrim(execution_profile) <> ''),
  execution_shape text NOT NULL CHECK (execution_shape IN ('local_bounded', 'remote_async_artifact', 'ops_bounded')),
  submission_mode text NOT NULL CHECK (submission_mode IN ('singleton', 'explicit_batch')),

  priority integer NOT NULL DEFAULT 50 CHECK (priority BETWEEN 0 AND 100),
  input_item_count integer CHECK (input_item_count IS NULL OR input_item_count > 0),

  status text NOT NULL CHECK (status IN ('pending', 'leased', 'retryable', 'succeeded', 'dead', 'cancelled')),
  runnable_at timestamptz NOT NULL DEFAULT NOW(),
  deadline_at timestamptz NULL,

  idempotency_key text NOT NULL CHECK (btrim(idempotency_key) <> ''),
  effect_key text NULL CHECK (effect_key IS NULL OR btrim(effect_key) <> ''),

  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),

  lease_owner text NULL CHECK (lease_owner IS NULL OR btrim(lease_owner) <> ''),
  lease_token uuid NULL,
  lease_expires_at timestamptz NULL,

  input_json jsonb NULL,
  input_artifact_id uuid NULL,
  output_contract_json jsonb NULL,

  terminal_result_id uuid NULL,
  last_error_class text NULL,
  last_error_text text NULL,

  created_at timestamptz NOT NULL DEFAULT NOW(),
  updated_at timestamptz NOT NULL DEFAULT NOW(),

  CHECK (attempt_count <= max_attempts),

  CHECK (
    submission_mode <> 'explicit_batch'
    OR input_item_count IS NOT NULL
  ),

  CHECK (
    (status = 'leased' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
    OR
    (status <> 'leased' AND lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL)
  ),

  CHECK (
    (status IN ('succeeded', 'dead', 'cancelled') AND terminal_result_id IS NOT NULL)
    OR
    (status IN ('pending', 'leased', 'retryable') AND terminal_result_id IS NULL)
  )
);

COMMENT ON TABLE runtime.jobs IS 'Durable bounded-work requests. Execution state only; not the owner mailbox.';
COMMENT ON COLUMN runtime.jobs.submission_mode IS 'Whether the producer submitted one singleton job or one explicit bounded batch job.';
COMMENT ON COLUMN runtime.jobs.input_item_count IS 'Minimal observable input cardinality for explicit batch jobs; usually null for singleton jobs.';
COMMENT ON COLUMN runtime.jobs.terminal_result_id IS 'Convenience pointer to the authoritative terminal handoff row in runtime.job_results.';

CREATE TABLE IF NOT EXISTS runtime.job_attempts (
  attempt_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id uuid NOT NULL,
  attempt_no integer NOT NULL CHECK (attempt_no >= 1),

  worker_id text NOT NULL CHECK (btrim(worker_id) <> ''),
  execution_profile text NOT NULL CHECK (btrim(execution_profile) <> ''),

  lease_token uuid NOT NULL,
  lease_expires_at timestamptz NOT NULL,

  status text NOT NULL CHECK (status IN ('leased', 'running', 'succeeded', 'retryable_error', 'terminal_error', 'expired', 'abandoned')),
  started_at timestamptz NOT NULL DEFAULT NOW(),
  heartbeat_at timestamptz NOT NULL DEFAULT NOW(),
  completed_at timestamptz NULL,

  error_class text NULL,
  error_text text NULL,
  telemetry_json jsonb NULL,
  provider_receipt_json jsonb NULL,
  probe_summary_json jsonb NULL,
  attempt_output_artifact_id uuid NULL,

  created_at timestamptz NOT NULL DEFAULT NOW(),
  updated_at timestamptz NOT NULL DEFAULT NOW(),

  CHECK (
    (status IN ('leased', 'running') AND completed_at IS NULL)
    OR
    (status IN ('succeeded', 'retryable_error', 'terminal_error', 'expired', 'abandoned') AND completed_at IS NOT NULL)
  ),

  CHECK (heartbeat_at >= started_at)
);

COMMENT ON TABLE runtime.job_attempts IS 'Durable per-attempt execution history. Active attempts are fenced by lease_token.';

CREATE TABLE IF NOT EXISTS runtime.job_results (
  result_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id uuid NOT NULL,
  attempt_id uuid NULL,

  result_owner text NOT NULL CHECK (btrim(result_owner) <> ''),
  result_route text NOT NULL CHECK (btrim(result_route) <> ''),
  result_kind text NOT NULL CHECK (result_kind IN ('succeeded', 'dead', 'cancelled')),

  result_json jsonb NULL,
  output_artifact_id uuid NULL,
  error_class text NULL,
  error_text text NULL,

  created_at timestamptz NOT NULL DEFAULT NOW(),
  claimed_at timestamptz NULL,
  consumed_at timestamptz NULL,

  CHECK (claimed_at IS NULL OR claimed_at >= created_at),
  CHECK (consumed_at IS NULL OR consumed_at >= created_at),
  CHECK (claimed_at IS NULL OR consumed_at IS NULL OR consumed_at >= claimed_at)
);

COMMENT ON TABLE runtime.job_results IS 'Authoritative terminal handoff rows. Payload is write-once; only delivery acknowledgements mutate.';

CREATE TABLE IF NOT EXISTS runtime.artifacts (
  artifact_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  artifact_kind text NOT NULL CHECK (btrim(artifact_kind) <> ''),
  storage_uri text NOT NULL CHECK (btrim(storage_uri) <> ''),
  content_hash text NOT NULL CHECK (btrim(content_hash) <> ''),
  byte_size bigint NOT NULL CHECK (byte_size >= 0),
  mime_type text NULL,
  schema_ref text NULL,
  retention_class text NOT NULL CHECK (retention_class IN ('ephemeral', 'durable', 'published')),

  producer_system text NOT NULL CHECK (btrim(producer_system) <> ''),
  created_by_job_id uuid NULL,
  created_by_attempt_id uuid NULL,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,

  created_at timestamptz NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE runtime.artifacts IS 'Registry of durable large input/output boundaries. Stores references and metadata, not blobs.';
COMMENT ON COLUMN runtime.artifacts.storage_uri IS 'Canonical durable object location, e.g. b2://bucket/key.';

ALTER TABLE runtime.jobs
  ADD CONSTRAINT jobs_producer_idempotency_key UNIQUE (producer_system, idempotency_key);

ALTER TABLE runtime.job_attempts
  ADD CONSTRAINT job_attempts_job_attempt_no_key UNIQUE (job_id, attempt_no),
  ADD CONSTRAINT job_attempts_job_attempt_id_key UNIQUE (job_id, attempt_id),
  ADD CONSTRAINT job_attempts_job_lease_token_key UNIQUE (job_id, lease_token);

ALTER TABLE runtime.job_results
  ADD CONSTRAINT job_results_one_per_job UNIQUE (job_id),
  ADD CONSTRAINT job_results_job_result_id_key UNIQUE (job_id, result_id);

ALTER TABLE runtime.artifacts
  ADD CONSTRAINT artifacts_storage_uri_key UNIQUE (storage_uri);

ALTER TABLE runtime.job_attempts
  ADD CONSTRAINT job_attempts_job_id_fkey
  FOREIGN KEY (job_id)
  REFERENCES runtime.jobs (job_id)
  ON DELETE RESTRICT;

ALTER TABLE runtime.jobs
  ADD CONSTRAINT jobs_input_artifact_id_fkey
  FOREIGN KEY (input_artifact_id)
  REFERENCES runtime.artifacts (artifact_id)
  ON DELETE RESTRICT;

ALTER TABLE runtime.job_attempts
  ADD CONSTRAINT job_attempts_attempt_output_artifact_id_fkey
  FOREIGN KEY (attempt_output_artifact_id)
  REFERENCES runtime.artifacts (artifact_id)
  ON DELETE RESTRICT;

ALTER TABLE runtime.job_results
  ADD CONSTRAINT job_results_job_id_fkey
  FOREIGN KEY (job_id)
  REFERENCES runtime.jobs (job_id)
  ON DELETE RESTRICT;

ALTER TABLE runtime.job_results
  ADD CONSTRAINT job_results_output_artifact_id_fkey
  FOREIGN KEY (output_artifact_id)
  REFERENCES runtime.artifacts (artifact_id)
  ON DELETE RESTRICT;

ALTER TABLE runtime.job_results
  ADD CONSTRAINT job_results_attempt_id_fkey
  FOREIGN KEY (job_id, attempt_id)
  REFERENCES runtime.job_attempts (job_id, attempt_id)
  DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE runtime.artifacts
  ADD CONSTRAINT artifacts_created_by_job_id_fkey
  FOREIGN KEY (created_by_job_id)
  REFERENCES runtime.jobs (job_id)
  ON DELETE RESTRICT;

ALTER TABLE runtime.artifacts
  ADD CONSTRAINT artifacts_created_by_attempt_id_fkey
  FOREIGN KEY (created_by_attempt_id)
  REFERENCES runtime.job_attempts (attempt_id)
  ON DELETE RESTRICT;

ALTER TABLE runtime.jobs
  ADD CONSTRAINT jobs_terminal_result_id_fkey
  FOREIGN KEY (job_id, terminal_result_id)
  REFERENCES runtime.job_results (job_id, result_id)
  DEFERRABLE INITIALLY DEFERRED;

CREATE UNIQUE INDEX IF NOT EXISTS jobs_effect_key_uidx
  ON runtime.jobs (producer_system, effect_key)
  WHERE effect_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS jobs_terminal_result_id_uidx
  ON runtime.jobs (terminal_result_id)
  WHERE terminal_result_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS job_attempts_active_one_per_job_uidx
  ON runtime.job_attempts (job_id)
  WHERE status IN ('leased', 'running');

CREATE INDEX IF NOT EXISTS jobs_claim_idx
  ON runtime.jobs (execution_profile, queue_class, priority DESC, runnable_at ASC, job_id)
  WHERE status IN ('pending', 'retryable');

CREATE INDEX IF NOT EXISTS jobs_deadline_expiry_idx
  ON runtime.jobs (deadline_at ASC, job_id)
  WHERE status IN ('pending', 'retryable') AND deadline_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS jobs_inflight_by_producer_idx
  ON runtime.jobs (producer_system, execution_profile, queue_class, job_id)
  WHERE status = 'leased';

CREATE INDEX IF NOT EXISTS jobs_stale_lease_idx
  ON runtime.jobs (lease_expires_at ASC, job_id)
  WHERE status = 'leased';

CREATE INDEX IF NOT EXISTS job_attempts_job_idx
  ON runtime.job_attempts (job_id, attempt_no DESC);

CREATE INDEX IF NOT EXISTS job_attempts_active_lease_idx
  ON runtime.job_attempts (lease_expires_at ASC, job_id)
  WHERE status IN ('leased', 'running');

CREATE INDEX IF NOT EXISTS job_results_ready_by_owner_idx
  ON runtime.job_results (result_owner, created_at ASC, job_id)
  WHERE consumed_at IS NULL;

CREATE INDEX IF NOT EXISTS job_results_unclaimed_by_owner_idx
  ON runtime.job_results (result_owner, created_at ASC, job_id)
  WHERE claimed_at IS NULL AND consumed_at IS NULL;

CREATE INDEX IF NOT EXISTS artifacts_created_by_job_idx
  ON runtime.artifacts (created_by_job_id, created_at DESC)
  WHERE created_by_job_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS artifacts_created_by_attempt_idx
  ON runtime.artifacts (created_by_attempt_id, created_at DESC)
  WHERE created_by_attempt_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS artifacts_content_hash_idx
  ON runtime.artifacts (content_hash);

CREATE OR REPLACE FUNCTION runtime.enforce_job_status_transitions()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'pending' THEN
      RAISE EXCEPTION 'runtime.jobs rows must be inserted in pending state';
    END IF;
    RETURN NEW;
  END IF;

  IF NEW.status = OLD.status THEN
    RETURN NEW;
  END IF;

  IF (
    (OLD.status = 'pending' AND NEW.status IN ('leased', 'dead', 'cancelled'))
    OR (OLD.status = 'retryable' AND NEW.status IN ('leased', 'dead', 'cancelled'))
    OR (OLD.status = 'leased' AND NEW.status IN ('retryable', 'succeeded', 'dead'))
  ) THEN
    RETURN NEW;
  END IF;

  RAISE EXCEPTION 'invalid runtime.jobs status transition: % -> % for job %', OLD.status, NEW.status, OLD.job_id;
END;
$$;

CREATE OR REPLACE FUNCTION runtime.enforce_job_attempt_status_transitions()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'leased' THEN
      RAISE EXCEPTION 'runtime.job_attempts rows must be inserted in leased state';
    END IF;
    RETURN NEW;
  END IF;

  IF NEW.status = OLD.status THEN
    RETURN NEW;
  END IF;

  IF (
    (OLD.status = 'leased' AND NEW.status IN ('running', 'succeeded', 'retryable_error', 'terminal_error', 'expired', 'abandoned'))
    OR (OLD.status = 'running' AND NEW.status IN ('succeeded', 'retryable_error', 'terminal_error', 'expired', 'abandoned'))
  ) THEN
    RETURN NEW;
  END IF;

  RAISE EXCEPTION 'invalid runtime.job_attempts status transition: % -> % for attempt %', OLD.status, NEW.status, OLD.attempt_id;
END;
$$;

CREATE OR REPLACE FUNCTION runtime.enforce_job_results_payload_immutability()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.job_id IS DISTINCT FROM OLD.job_id
     OR NEW.attempt_id IS DISTINCT FROM OLD.attempt_id
     OR NEW.result_owner IS DISTINCT FROM OLD.result_owner
     OR NEW.result_route IS DISTINCT FROM OLD.result_route
     OR NEW.result_kind IS DISTINCT FROM OLD.result_kind
     OR NEW.result_json IS DISTINCT FROM OLD.result_json
     OR NEW.output_artifact_id IS DISTINCT FROM OLD.output_artifact_id
     OR NEW.error_class IS DISTINCT FROM OLD.error_class
     OR NEW.error_text IS DISTINCT FROM OLD.error_text
     OR NEW.created_at IS DISTINCT FROM OLD.created_at
  THEN
    RAISE EXCEPTION 'runtime.job_results payload fields are immutable once written';
  END IF;

  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION runtime.enforce_terminal_handoff_consistency()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  v_job_id uuid := CASE WHEN TG_OP = 'DELETE' THEN OLD.job_id ELSE NEW.job_id END;
  v_job runtime.jobs%ROWTYPE;
  v_result runtime.job_results%ROWTYPE;
  v_result_count integer;
BEGIN
  SELECT *
    INTO v_job
    FROM runtime.jobs
   WHERE job_id = v_job_id;

  IF NOT FOUND THEN
    RETURN NULL;
  END IF;

  SELECT COUNT(*)
    INTO v_result_count
    FROM runtime.job_results
   WHERE job_id = v_job.job_id;

  IF v_job.status IN ('succeeded', 'dead', 'cancelled') THEN
    IF v_result_count <> 1 THEN
      RAISE EXCEPTION 'terminal job % must have exactly one runtime.job_results row', v_job.job_id;
    END IF;

    SELECT *
      INTO v_result
      FROM runtime.job_results
     WHERE job_id = v_job.job_id;

    IF v_job.terminal_result_id IS NULL OR v_result.result_id <> v_job.terminal_result_id THEN
      RAISE EXCEPTION 'job % terminal_result_id must point to its unique runtime.job_results row', v_job.job_id;
    END IF;

    IF v_result.result_owner <> v_job.result_owner OR v_result.result_route <> v_job.result_route THEN
      RAISE EXCEPTION 'job % terminal result owner/route must match runtime.jobs', v_job.job_id;
    END IF;

    IF v_result.result_kind <> v_job.status THEN
      RAISE EXCEPTION 'job % terminal result kind % must match job status %', v_job.job_id, v_result.result_kind, v_job.status;
    END IF;
  ELSE
    IF v_job.terminal_result_id IS NOT NULL THEN
      RAISE EXCEPTION 'non-terminal job % must not carry terminal_result_id', v_job.job_id;
    END IF;

    IF v_result_count <> 0 THEN
      RAISE EXCEPTION 'non-terminal job % must not have runtime.job_results rows', v_job.job_id;
    END IF;
  END IF;

  RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION runtime.enforce_leased_job_attempt_consistency()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  v_job_id uuid := CASE WHEN TG_OP = 'DELETE' THEN OLD.job_id ELSE NEW.job_id END;
  v_job runtime.jobs%ROWTYPE;
  v_active_count integer;
  v_matching_count integer;
BEGIN
  SELECT *
    INTO v_job
    FROM runtime.jobs
   WHERE job_id = v_job_id;

  IF NOT FOUND THEN
    RETURN NULL;
  END IF;

  SELECT COUNT(*)
    INTO v_active_count
    FROM runtime.job_attempts
   WHERE job_id = v_job.job_id
     AND status IN ('leased', 'running');

  IF v_job.status = 'leased' THEN
    SELECT COUNT(*)
      INTO v_matching_count
      FROM runtime.job_attempts
     WHERE job_id = v_job.job_id
       AND lease_token = v_job.lease_token
       AND status IN ('leased', 'running');

    IF v_matching_count <> 1 THEN
      RAISE EXCEPTION 'leased job % must have exactly one active attempt with matching lease_token', v_job.job_id;
    END IF;

    IF v_active_count <> 1 THEN
      RAISE EXCEPTION 'leased job % must have exactly one active attempt', v_job.job_id;
    END IF;
  ELSE
    IF v_active_count <> 0 THEN
      RAISE EXCEPTION 'non-leased job % must not have active attempts', v_job.job_id;
    END IF;
  END IF;

  RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS jobs_status_transition_trg ON runtime.jobs;
CREATE TRIGGER jobs_status_transition_trg
BEFORE INSERT OR UPDATE ON runtime.jobs
FOR EACH ROW
EXECUTE FUNCTION runtime.enforce_job_status_transitions();

DROP TRIGGER IF EXISTS jobs_set_updated_at ON runtime.jobs;
CREATE TRIGGER jobs_set_updated_at
BEFORE UPDATE ON runtime.jobs
FOR EACH ROW
EXECUTE FUNCTION runtime.set_updated_at();

DROP TRIGGER IF EXISTS job_attempts_status_transition_trg ON runtime.job_attempts;
CREATE TRIGGER job_attempts_status_transition_trg
BEFORE INSERT OR UPDATE ON runtime.job_attempts
FOR EACH ROW
EXECUTE FUNCTION runtime.enforce_job_attempt_status_transitions();

DROP TRIGGER IF EXISTS job_attempts_set_updated_at ON runtime.job_attempts;
CREATE TRIGGER job_attempts_set_updated_at
BEFORE UPDATE ON runtime.job_attempts
FOR EACH ROW
EXECUTE FUNCTION runtime.set_updated_at();

DROP TRIGGER IF EXISTS job_results_payload_immutability_trg ON runtime.job_results;
CREATE TRIGGER job_results_payload_immutability_trg
BEFORE UPDATE ON runtime.job_results
FOR EACH ROW
EXECUTE FUNCTION runtime.enforce_job_results_payload_immutability();

DROP TRIGGER IF EXISTS jobs_terminal_handoff_consistency_trg ON runtime.jobs;
CREATE CONSTRAINT TRIGGER jobs_terminal_handoff_consistency_trg
AFTER INSERT OR UPDATE ON runtime.jobs
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION runtime.enforce_terminal_handoff_consistency();

DROP TRIGGER IF EXISTS job_results_terminal_handoff_consistency_trg ON runtime.job_results;
CREATE CONSTRAINT TRIGGER job_results_terminal_handoff_consistency_trg
AFTER INSERT OR UPDATE OR DELETE ON runtime.job_results
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION runtime.enforce_terminal_handoff_consistency();

DROP TRIGGER IF EXISTS jobs_leased_attempt_consistency_trg ON runtime.jobs;
CREATE CONSTRAINT TRIGGER jobs_leased_attempt_consistency_trg
AFTER INSERT OR UPDATE ON runtime.jobs
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION runtime.enforce_leased_job_attempt_consistency();

DROP TRIGGER IF EXISTS job_attempts_leased_attempt_consistency_trg ON runtime.job_attempts;
CREATE CONSTRAINT TRIGGER job_attempts_leased_attempt_consistency_trg
AFTER INSERT OR UPDATE OR DELETE ON runtime.job_attempts
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION runtime.enforce_leased_job_attempt_consistency();

COMMIT;
