-- Rendered feed XML, one row per feed: built once per change, served as stored text.
CREATE TABLE IF NOT EXISTS feed_xml (
  feed TEXT PRIMARY KEY,
  version TEXT NOT NULL,
  etag TEXT NOT NULL,
  xml TEXT NOT NULL,
  built_at INTEGER NOT NULL
) WITHOUT ROWID;
