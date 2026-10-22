-- Add durable active-run tracking for Pi worker process control.
--
-- Runtime-controller owns the generic job/attempt lease state.  This table is
-- Pi-specific operational state used by tg-gateway `/stop` to request and
-- safely signal the local Pi subprocess by PID plus proc start ticks.

BEGIN;

CREATE TABLE IF NOT EXISTS pi_bot.active_runs (
  job_id uuid PRIMARY KEY,
  attempt_id uuid NOT NULL,
  lease_token uuid NOT NULL,
  worker_id text NOT NULL CHECK (btrim(worker_id) <> ''),
  chat_id bigint NOT NULL,
  topic_id bigint NOT NULL,
  session_ref text NOT NULL CHECK (btrim(session_ref) <> ''),
  session_path text NOT NULL CHECK (btrim(session_path) <> ''),
  child_pid integer CHECK (child_pid IS NULL OR child_pid > 0),
  child_start_ticks text,
  state text NOT NULL DEFAULT 'starting'
    CHECK (state IN ('starting', 'running', 'stopping', 'finished')),
  stop_requested_at timestamptz,
  stop_reason text,
  outcome text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);

COMMENT ON TABLE pi_bot.active_runs IS 'Pi-specific active subprocess ledger for safe Telegram /stop handling.';
COMMENT ON COLUMN pi_bot.active_runs.child_start_ticks IS 'Value from /proc/<pid>/stat field 22, used with PID to avoid signalling a reused process id.';
COMMENT ON COLUMN pi_bot.active_runs.state IS 'starting/running/stopping are active states; finished rows are retained as short-term stop/debug evidence.';

CREATE INDEX IF NOT EXISTS pi_bot_active_runs_topic_state_idx
  ON pi_bot.active_runs (chat_id, topic_id, state, updated_at DESC);

CREATE INDEX IF NOT EXISTS pi_bot_active_runs_worker_state_idx
  ON pi_bot.active_runs (worker_id, state, updated_at DESC);

DROP TRIGGER IF EXISTS pi_bot_active_runs_set_updated_at ON pi_bot.active_runs;
CREATE TRIGGER pi_bot_active_runs_set_updated_at
  BEFORE UPDATE ON pi_bot.active_runs
  FOR EACH ROW
  EXECUTE FUNCTION runtime.set_updated_at();

COMMIT;
