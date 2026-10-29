-- Bounded health reads: newest handoff and oldest unsettled handoff.
CREATE INDEX source_handoffs_health_latest_idx ON tg_gateway.source_handoffs (created_at DESC);
CREATE INDEX source_handoffs_health_pending_idx ON tg_gateway.source_handoffs (created_at)
    WHERE state IN ('pending', 'staged');
