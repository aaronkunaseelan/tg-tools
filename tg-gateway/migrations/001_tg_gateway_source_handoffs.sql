-- Wave 1: gateway-owned transactional source-handoff outbox.
-- Migration authority: app-owned tg_gateway.* tables are versioned HERE
-- (tg-tools), not in the unversioned deployed runtime-controller tree.
-- Apply requires explicit operator approval.

CREATE TABLE IF NOT EXISTS tg_gateway.source_handoffs (
    handoff_id       BIGSERIAL PRIMARY KEY,
    update_uid       TEXT NOT NULL UNIQUE,
    source_key       TEXT NOT NULL,
    bot_identity     TEXT NOT NULL,
    route_id         TEXT NOT NULL,
    route_kind       TEXT NOT NULL,
    rule_version     TEXT NOT NULL,
    -- versioned admission intent: frozen receipt/job payloads plus
    -- attachment spec; never raw private text, never runtime attempt state.
    intent_json      JSONB NOT NULL,
    state            TEXT NOT NULL DEFAULT 'pending'
                     CHECK (state IN ('pending', 'staged', 'submitted', 'terminal_rejected')),
    staged_path      TEXT,
    staged_sha256    TEXT,
    staged_size      BIGINT,
    receipt_id       TEXT,
    job_id           TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    lease_owner      TEXT,
    lease_token      TEXT,
    lease_expires_at TIMESTAMPTZ,
    last_error       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS source_handoffs_pending_idx
    ON tg_gateway.source_handoffs (state, lease_expires_at)
    WHERE state IN ('pending', 'staged');

-- Rollback:
--   DROP INDEX IF EXISTS tg_gateway.source_handoffs_pending_idx;
--   DROP TABLE IF EXISTS tg_gateway.source_handoffs;
