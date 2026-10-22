-- Pi bot state: the Pi gateway and workers are gone, nothing reads these tables.
-- rich_capabilities was the Pi gateway's draft-capability cache.
-- Rollback requires restoring the dropped schema and data from backup.
DROP SCHEMA IF EXISTS pi_bot CASCADE;
DROP TABLE IF EXISTS tg_gateway.rich_capabilities;
