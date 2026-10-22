-- Stored feeds as gzip (base64 text, ~5x smaller than the XML): D1 result decoding dominated the
-- GET's CPU. Clients that accept gzip get the stored bytes as-is.
CREATE TABLE IF NOT EXISTS feed_gz (
  feed TEXT PRIMARY KEY,
  version TEXT NOT NULL,
  etag TEXT NOT NULL,
  gz TEXT NOT NULL,
  built_at INTEGER NOT NULL
) WITHOUT ROWID;
