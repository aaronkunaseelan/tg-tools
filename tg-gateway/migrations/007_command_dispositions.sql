-- Existing handled markers remain handled. New claims record effect uncertainty
-- until the command returns; never automatically repeat an uncertain command.
ALTER TABLE tg_gateway.handled_updates ADD COLUMN disposition TEXT NOT NULL DEFAULT 'handled'
    CHECK (disposition IN ('handled', 'uncertain'));
