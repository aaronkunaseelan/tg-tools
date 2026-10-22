BEGIN;

CREATE TABLE IF NOT EXISTS runtime.delivery_outbox (
  delivery_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  result_id uuid NULL,
  job_id uuid NULL,

  result_owner text NOT NULL CHECK (btrim(result_owner) <> ''),
  result_route text NOT NULL CHECK (btrim(result_route) <> ''),

  delivery_provider text NOT NULL CHECK (btrim(delivery_provider) <> ''),
  delivery_kind text NOT NULL CHECK (btrim(delivery_kind) <> ''),
  delivery_key text NOT NULL CHECK (btrim(delivery_key) <> ''),

  priority integer NOT NULL DEFAULT 50 CHECK (priority BETWEEN 0 AND 100),
  status text NOT NULL CHECK (status IN ('pending', 'claimed', 'retryable', 'sent', 'dead', 'cancelled')),
  runnable_at timestamptz NOT NULL DEFAULT NOW(),
  deadline_at timestamptz NULL,

  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts >= 1),

  claim_owner text NULL CHECK (claim_owner IS NULL OR btrim(claim_owner) <> ''),
  claim_token uuid NULL,
  claim_expires_at timestamptz NULL,

  payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  provider_receipt_json jsonb NULL,

  last_error_class text NULL,
  last_error_text text NULL,

  sent_at timestamptz NULL,
  dead_at timestamptz NULL,
  cancelled_at timestamptz NULL,

  created_at timestamptz NOT NULL DEFAULT NOW(),
  updated_at timestamptz NOT NULL DEFAULT NOW(),

  CHECK (attempt_count <= max_attempts),

  CHECK (
    (result_id IS NULL AND job_id IS NULL)
    OR
    (result_id IS NOT NULL AND job_id IS NOT NULL)
  ),

  CHECK (
    (status = 'claimed' AND claim_owner IS NOT NULL AND claim_token IS NOT NULL AND claim_expires_at IS NOT NULL)
    OR
    (status <> 'claimed' AND claim_owner IS NULL AND claim_token IS NULL AND claim_expires_at IS NULL)
  ),

  CHECK (
    (status = 'sent' AND sent_at IS NOT NULL)
    OR
    (status <> 'sent' AND sent_at IS NULL)
  ),

  CHECK (
    (status = 'dead' AND dead_at IS NOT NULL)
    OR
    (status <> 'dead' AND dead_at IS NULL)
  ),

  CHECK (
    (status = 'cancelled' AND cancelled_at IS NOT NULL)
    OR
    (status <> 'cancelled' AND cancelled_at IS NULL)
  )
);

COMMENT ON TABLE runtime.delivery_outbox IS 'Durable provider delivery work. Delivery failures are retried here and do not rerun execution.';
COMMENT ON COLUMN runtime.delivery_outbox.delivery_key IS 'Producer/owner scoped idempotency key for one intended provider delivery.';
COMMENT ON COLUMN runtime.delivery_outbox.payload_json IS 'Provider-neutral delivery payload. Large bytes should be referenced through artifacts or storage URIs.';
COMMENT ON COLUMN runtime.delivery_outbox.provider_receipt_json IS 'Provider acknowledgement stored when delivery is sent.';

ALTER TABLE runtime.delivery_outbox
  ADD CONSTRAINT delivery_outbox_owner_key UNIQUE (result_owner, delivery_key);

ALTER TABLE runtime.delivery_outbox
  ADD CONSTRAINT delivery_outbox_result_id_fkey
  FOREIGN KEY (result_id)
  REFERENCES runtime.job_results (result_id)
  ON DELETE RESTRICT;

ALTER TABLE runtime.delivery_outbox
  ADD CONSTRAINT delivery_outbox_job_result_id_fkey
  FOREIGN KEY (job_id, result_id)
  REFERENCES runtime.job_results (job_id, result_id)
  DEFERRABLE INITIALLY DEFERRED;

CREATE UNIQUE INDEX IF NOT EXISTS delivery_outbox_active_claim_uidx
  ON runtime.delivery_outbox (delivery_id)
  WHERE status = 'claimed';

CREATE INDEX IF NOT EXISTS delivery_outbox_claim_idx
  ON runtime.delivery_outbox (delivery_provider, delivery_kind, priority DESC, runnable_at ASC, delivery_id)
  WHERE status IN ('pending', 'retryable');

CREATE INDEX IF NOT EXISTS delivery_outbox_stale_claim_idx
  ON runtime.delivery_outbox (claim_expires_at ASC, delivery_id)
  WHERE status = 'claimed';

CREATE INDEX IF NOT EXISTS delivery_outbox_result_idx
  ON runtime.delivery_outbox (result_id)
  WHERE result_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS delivery_outbox_owner_status_idx
  ON runtime.delivery_outbox (result_owner, status, created_at DESC, delivery_id);

CREATE OR REPLACE FUNCTION runtime.enforce_delivery_outbox_status_transitions()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'pending' THEN
      RAISE EXCEPTION 'runtime.delivery_outbox rows must be inserted in pending state';
    END IF;
    RETURN NEW;
  END IF;

  IF NEW.status = OLD.status THEN
    RETURN NEW;
  END IF;

  IF (
    (OLD.status = 'pending' AND NEW.status IN ('claimed', 'dead', 'cancelled'))
    OR (OLD.status = 'retryable' AND NEW.status IN ('claimed', 'dead', 'cancelled'))
    OR (OLD.status = 'claimed' AND NEW.status IN ('retryable', 'sent', 'dead', 'cancelled'))
  ) THEN
    RETURN NEW;
  END IF;

  RAISE EXCEPTION 'invalid runtime.delivery_outbox status transition: % -> % for delivery %', OLD.status, NEW.status, OLD.delivery_id;
END;
$$;

DROP TRIGGER IF EXISTS delivery_outbox_status_transition_trg ON runtime.delivery_outbox;
CREATE TRIGGER delivery_outbox_status_transition_trg
BEFORE INSERT OR UPDATE ON runtime.delivery_outbox
FOR EACH ROW
EXECUTE FUNCTION runtime.enforce_delivery_outbox_status_transitions();

DROP TRIGGER IF EXISTS delivery_outbox_set_updated_at ON runtime.delivery_outbox;
CREATE TRIGGER delivery_outbox_set_updated_at
BEFORE UPDATE ON runtime.delivery_outbox
FOR EACH ROW
EXECUTE FUNCTION runtime.set_updated_at();

COMMIT;
