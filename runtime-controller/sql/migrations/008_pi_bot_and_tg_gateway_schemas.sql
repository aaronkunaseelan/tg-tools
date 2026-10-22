-- Additive pi_bot and tg_gateway schemas for the Pi greenfield refactor.
--
-- These tables support the tg-gateway transport layer and pi-worker
-- execution layer.  They live beside the existing runtime.* tables but
-- are owned by the Pi/gateway subsystem, not the generic runtime.
--
-- The runtime-controller already provides:
--   runtime.jobs, runtime.job_attempts, runtime.job_results,
--   runtime.artifacts, runtime.delivery_outbox,
--   runtime.source_receipts
--
-- This migration adds the domain-specific state that was previously
-- held in Pi's SQLite database.

BEGIN;

CREATE SCHEMA IF NOT EXISTS pi_bot;
CREATE SCHEMA IF NOT EXISTS tg_gateway;

-- Pi bot: topic bindings

CREATE TABLE IF NOT EXISTS pi_bot.topic_bindings (
  chat_id bigint NOT NULL,
  topic_id bigint NOT NULL,
  session_ref text NOT NULL CHECK (btrim(session_ref) <> ''),
  session_path text NOT NULL CHECK (btrim(session_path) <> ''),
  workdir text NOT NULL CHECK (btrim(workdir) <> ''),
  skill text,
  state text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'closed')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (chat_id, topic_id)
);

COMMENT ON TABLE pi_bot.topic_bindings IS 'Maps Telegram forum chat/topic pairs to Pi agent sessions.';
COMMENT ON COLUMN pi_bot.topic_bindings.session_ref IS 'Stable user-facing session label, e.g. pi-tg-gateb-worker.';
COMMENT ON COLUMN pi_bot.topic_bindings.session_path IS 'Absolute path to the Pi session directory.';
COMMENT ON COLUMN pi_bot.topic_bindings.workdir IS 'Working directory for the Pi subprocess.';
COMMENT ON COLUMN pi_bot.topic_bindings.state IS 'open for active, closed when topic ends; closed rows are retained for history.';

CREATE INDEX IF NOT EXISTS pi_bot_topic_bindings_state_idx
  ON pi_bot.topic_bindings (state, updated_at DESC);

CREATE INDEX IF NOT EXISTS pi_bot_topic_bindings_session_idx
  ON pi_bot.topic_bindings (session_ref)
  WHERE state = 'open';

DROP TRIGGER IF EXISTS pi_bot_topic_bindings_set_updated_at ON pi_bot.topic_bindings;
CREATE TRIGGER pi_bot_topic_bindings_set_updated_at
  BEFORE UPDATE ON pi_bot.topic_bindings
  FOR EACH ROW
  EXECUTE FUNCTION runtime.set_updated_at();

-- Pi bot: callback tokens

CREATE TABLE IF NOT EXISTS pi_bot.callback_tokens (
  token text PRIMARY KEY CHECK (btrim(token) <> ''),
  chat_id bigint NOT NULL,
  topic_id bigint,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  expires_at timestamptz NOT NULL,
  consumed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE pi_bot.callback_tokens IS 'Server-side storage for Telegram inline-keyboard callback payloads. Telegram callback_data size is limited; this table holds the full payload indexed by a short opaque token.';
COMMENT ON COLUMN pi_bot.callback_tokens.payload IS 'Full server-side payload (command, args, state). Not visible on the wire.';
COMMENT ON COLUMN pi_bot.callback_tokens.expires_at IS 'Token lifetime; callback requests after expiry are rejected.';

CREATE INDEX IF NOT EXISTS pi_bot_callback_tokens_expiry_idx
  ON pi_bot.callback_tokens (expires_at ASC, created_at ASC)
  WHERE consumed_at IS NULL;

CREATE INDEX IF NOT EXISTS pi_bot_callback_tokens_chat_topic_idx
  ON pi_bot.callback_tokens (chat_id, topic_id, created_at DESC);

-- Pi bot: prompt inputs

CREATE TABLE IF NOT EXISTS pi_bot.prompt_inputs (
  input_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_key text NOT NULL UNIQUE CHECK (btrim(source_key) <> ''),
  chat_id bigint NOT NULL,
  topic_id bigint NOT NULL,
  anchor_message_id bigint NOT NULL,
  prompt_text text,
  prompt_text_sha256 text NOT NULL CHECK (btrim(prompt_text_sha256) <> ''),
  prompt_text_length integer NOT NULL CHECK (prompt_text_length >= 0),
  attachment_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
  retention_expires_at timestamptz,
  consumed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE pi_bot.prompt_inputs IS 'Restricted durable store for raw Pi prompt bodies. The runtime job input_json carries only sha256, length, and an optional promptRef; workers retrieve the full text from this table.';
COMMENT ON COLUMN pi_bot.prompt_inputs.source_key IS 'Telegram source key linking this input to its source receipt, e.g. tg:pi-bot:<chat_id>:<message_id>.';
COMMENT ON COLUMN pi_bot.prompt_inputs.prompt_text IS 'Raw UTF-8 prompt body. Nullable because some inputs may be attachment-only.';
COMMENT ON COLUMN pi_bot.prompt_inputs.retention_expires_at IS 'Time after which the raw text may be deleted; set by the worker after consumption.';

CREATE INDEX IF NOT EXISTS pi_bot_prompt_inputs_retention_idx
  ON pi_bot.prompt_inputs (retention_expires_at ASC, created_at ASC)
  WHERE retention_expires_at IS NOT NULL AND consumed_at IS NULL;

CREATE INDEX IF NOT EXISTS pi_bot_prompt_inputs_consumed_idx
  ON pi_bot.prompt_inputs (consumed_at ASC, created_at ASC)
  WHERE consumed_at IS NOT NULL;

-- Pi bot: artifacts

CREATE TABLE IF NOT EXISTS pi_bot.artifacts (
  session_ref text NOT NULL CHECK (btrim(session_ref) <> ''),
  path text NOT NULL CHECK (btrim(path) <> ''),
  kind text NOT NULL CHECK (btrim(kind) <> ''),
  source text NOT NULL CHECK (source IN ('mutation_hook', 'tool_media', 'session_scan')),
  display_name text,
  created_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (session_ref, path)
);

COMMENT ON TABLE pi_bot.artifacts IS 'Per-session artifact index for Pi tool output and mutation-hook files. Workers update this after each run; the gateway reads it to serve /recent and artifact delivery.';
COMMENT ON COLUMN pi_bot.artifacts.path IS 'Path relative to session workdir or absolute session directory path.';
COMMENT ON COLUMN pi_bot.artifacts.kind IS 'Artifact kind: image, audio, markdown, etc.';

CREATE INDEX IF NOT EXISTS pi_bot_artifacts_session_seen_idx
  ON pi_bot.artifacts (session_ref, last_seen_at DESC);

-- TG Gateway: handled updates

CREATE TABLE IF NOT EXISTS tg_gateway.handled_updates (
  update_uid text PRIMARY KEY CHECK (btrim(update_uid) <> ''),
  source_system text NOT NULL DEFAULT 'telegram' CHECK (btrim(source_system) <> ''),
  handled_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE tg_gateway.handled_updates IS 'Inbound update idempotency guard. Before any work is done for an update the gateway records its Telegram update_id here; if the gateway replays the update stream it skips already-handled ids.';

-- TG Gateway: flood state

CREATE TABLE IF NOT EXISTS tg_gateway.flood_state (
  chat_id bigint PRIMARY KEY,
  retry_after_ts timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE tg_gateway.flood_state IS 'Per-chat flood/backoff state. When Telegram returns a 429 with retry_after the gateway upserts this row and stalls all sends to that chat until the cooldown expires.';

DROP TRIGGER IF EXISTS tg_gateway_flood_state_set_updated_at ON tg_gateway.flood_state;
CREATE TRIGGER tg_gateway_flood_state_set_updated_at
  BEFORE UPDATE ON tg_gateway.flood_state
  FOR EACH ROW
  EXECUTE FUNCTION runtime.set_updated_at();

-- TG Gateway: sent messages

CREATE TABLE IF NOT EXISTS tg_gateway.sent_messages (
  delivery_id uuid PRIMARY KEY,
  chat_id bigint NOT NULL,
  topic_id bigint,
  telegram_message_id bigint NOT NULL,
  delivery_key text NOT NULL CHECK (btrim(delivery_key) <> ''),
  sent_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (delivery_key)
);

COMMENT ON TABLE tg_gateway.sent_messages IS 'Maps runtime.delivery_outbox delivery ids and keys to sent Telegram message ids. The delivery drain checks this table before sending to prevent double-posts on re-drain.';
COMMENT ON COLUMN tg_gateway.sent_messages.delivery_key IS 'Same value as runtime.delivery_outbox.delivery_key; used for idempotent re-drain.';

CREATE INDEX IF NOT EXISTS tg_gateway_sent_messages_chat_idx
  ON tg_gateway.sent_messages (chat_id, topic_id, sent_at DESC);

-- TG Gateway: Rich capabilities

CREATE TABLE IF NOT EXISTS tg_gateway.rich_capabilities (
  chat_id bigint NOT NULL,
  topic_id bigint,
  final_rich_supported boolean,
  draft_rich_supported boolean,
  checked_at timestamptz NOT NULL DEFAULT now(),
  last_error_class text,
  PRIMARY KEY (chat_id, topic_id)
);

COMMENT ON TABLE tg_gateway.rich_capabilities IS 'Observed Telegram Rich (BlockMessage) support per chat/topic. Populated by a live capability probe; the renderer reads this to decide whether to use Rich or fall back to HTML/document delivery.';
COMMENT ON COLUMN tg_gateway.rich_capabilities.final_rich_supported IS 'Whether sendRichMessage works for this chat/topic.';
COMMENT ON COLUMN tg_gateway.rich_capabilities.draft_rich_supported IS 'Whether sendRichMessageDraft works for this chat/topic. Null until proven.';

COMMIT;
