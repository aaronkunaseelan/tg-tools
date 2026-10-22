-- Provider effect markers only; runtime.delivery_outbox owns scheduling/retries.
-- Apply before running a gateway built with the dispatch guard. Never expire a
-- marker automatically: absence of a receipt does not prove absence of a send.
CREATE SCHEMA IF NOT EXISTS tg_gateway;
CREATE TABLE IF NOT EXISTS tg_gateway.delivery_dispatches (
    delivery_key text NOT NULL CHECK (btrim(delivery_key) <> ''),
    delivery_id uuid PRIMARY KEY,
    dispatch_token uuid NOT NULL,
    dispatched_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- The controller's delivery UUID already binds owner/key. Preserve all historical
-- receipts by UUID while allowing different owners to use the same text key.
ALTER TABLE tg_gateway.sent_messages DROP CONSTRAINT IF EXISTS sent_messages_delivery_key_key;
CREATE INDEX IF NOT EXISTS sent_messages_delivery_key_idx ON tg_gateway.sent_messages (delivery_key);
