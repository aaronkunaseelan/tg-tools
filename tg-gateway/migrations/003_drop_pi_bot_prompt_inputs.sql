-- Destructive hard cut: old prompt references are no longer supported.
-- Rollback requires restoring the dropped schema and data from backup.
DROP TABLE IF EXISTS pi_bot.prompt_inputs;
