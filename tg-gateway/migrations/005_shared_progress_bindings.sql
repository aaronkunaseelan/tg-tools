-- Gateway projection state only. Canonical execution stays in runtime.*.
CREATE TABLE tg_gateway.progress_bindings (
    binding_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    handoff_id bigint NOT NULL REFERENCES tg_gateway.source_handoffs(handoff_id),
    bot_identity text NOT NULL,
    source_key text NOT NULL,
    target_json jsonb NOT NULL,
    message_id bigint,
    revision bigint NOT NULL DEFAULT 0,
    desired_payload jsonb,
    pending_delivery_id uuid,
    acknowledged_revision bigint NOT NULL DEFAULT 0,
    next_refresh_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    closed boolean NOT NULL DEFAULT false,
    quarantined boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (bot_identity, source_key)
);
CREATE INDEX progress_bindings_due ON tg_gateway.progress_bindings(bot_identity,next_refresh_at)
    WHERE NOT closed AND NOT quarantined;
