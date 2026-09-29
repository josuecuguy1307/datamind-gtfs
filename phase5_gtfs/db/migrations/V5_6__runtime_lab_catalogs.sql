CREATE TABLE IF NOT EXISTS gtfs_work.runtime_catalog_items (
  catalog_key TEXT NOT NULL,
  item_code TEXT NOT NULL,
  item_name TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  source TEXT NOT NULL DEFAULT 'manual',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (catalog_key, item_code)
);

CREATE INDEX IF NOT EXISTS idx_runtime_catalog_items_catalog
  ON gtfs_work.runtime_catalog_items(catalog_key, is_active, item_code);

