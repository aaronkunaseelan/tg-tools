-- Destructive hard cut: old generic source references are no longer supported.
-- Rollback requires restoring the dropped schema and data from backup.
DROP TABLE IF EXISTS tg_gateway.private_inputs;
