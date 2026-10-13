-- Readers pin one generation throughout a request.
CREATE TABLE IF NOT EXISTS catalog_generations (
  seq INTEGER PRIMARY KEY,
  generation TEXT NOT NULL UNIQUE,
  manifest_key TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('staging','ready')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS catalog_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  active_seq INTEGER REFERENCES catalog_generations(seq)
);
INSERT OR IGNORE INTO catalog_state(id, active_seq) VALUES(1, NULL);
CREATE TABLE IF NOT EXISTS catalog_records (
  kind TEXT NOT NULL,
  record_key TEXT NOT NULL,
  from_seq INTEGER NOT NULL,
  to_seq INTEGER,
  data TEXT NOT NULL CHECK (json_valid(data)),
  content_hash TEXT NOT NULL,
  PRIMARY KEY(kind, record_key, from_seq)
);
CREATE INDEX IF NOT EXISTS catalog_visible ON catalog_records(kind, from_seq, to_seq);
CREATE INDEX IF NOT EXISTS catalog_numeric_id ON catalog_records(kind, json_extract(data,'$.id'), from_seq);
CREATE INDEX IF NOT EXISTS catalog_source ON catalog_records(kind, json_extract(data,'$.source_id'), from_seq);
CREATE INDEX IF NOT EXISTS catalog_source_fk ON catalog_records(kind, json_extract(data,'$.source_fk'), from_seq);
CREATE INDEX IF NOT EXISTS catalog_bundle ON catalog_records(kind, json_extract(data,'$.bundle_id'), from_seq);
CREATE INDEX IF NOT EXISTS catalog_bundle_fk ON catalog_records(kind, json_extract(data,'$.bundle_fk'), from_seq);
CREATE INDEX IF NOT EXISTS catalog_package_fk ON catalog_records(kind, json_extract(data,'$.package_fk'), from_seq);
CREATE INDEX IF NOT EXISTS catalog_patch_fk ON catalog_records(kind, json_extract(data,'$.patch_fk'), from_seq);
CREATE INDEX IF NOT EXISTS catalog_name ON catalog_records(kind, json_extract(data,'$.name'), from_seq);
CREATE INDEX IF NOT EXISTS catalog_url ON catalog_records(kind, json_extract(data,'$.url'), from_seq);
CREATE INDEX IF NOT EXISTS catalog_release_date ON catalog_records(kind, json_extract(data,'$.published_at'), from_seq);
CREATE INDEX IF NOT EXISTS catalog_latest ON catalog_records(kind, json_extract(data,'$.is_latest'), from_seq);
CREATE TRIGGER IF NOT EXISTS catalog_close_previous BEFORE INSERT ON catalog_records
BEGIN
  UPDATE catalog_records SET to_seq=NEW.from_seq
  WHERE kind=NEW.kind AND record_key=NEW.record_key
    AND from_seq<NEW.from_seq AND to_seq IS NULL;
END;
