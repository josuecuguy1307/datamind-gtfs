CREATE TABLE IF NOT EXISTS gtfs_work.agency_catalog (
  agency_id TEXT PRIMARY KEY,
  agency_name TEXT NOT NULL,
  agency_name_norm TEXT NOT NULL,
  agency_url TEXT NOT NULL DEFAULT 'https://example.com',
  agency_timezone TEXT NOT NULL DEFAULT 'America/Guayaquil',
  agency_lang TEXT DEFAULT 'es',
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_gtfs_work_agency_catalog_norm
  ON gtfs_work.agency_catalog(agency_name_norm);

CREATE TABLE IF NOT EXISTS gtfs_work.route_agency_links (
  route_id UUID PRIMARY KEY REFERENCES route_prod.routes(route_id) ON DELETE CASCADE,
  agency_id TEXT NOT NULL REFERENCES gtfs_work.agency_catalog(agency_id) ON DELETE RESTRICT,
  match_score DOUBLE PRECISION,
  match_method TEXT,
  source_operator_name TEXT,
  status TEXT NOT NULL DEFAULT 'linked',
  updated_by TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gtfs_work_route_agency_links_agency
  ON gtfs_work.route_agency_links(agency_id);

CREATE TABLE IF NOT EXISTS gtfs_work.agency_match_requests (
  request_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  route_id UUID NOT NULL REFERENCES route_prod.routes(route_id) ON DELETE CASCADE,
  source_operator_name TEXT,
  source_operator_norm TEXT,
  suggested_agency_id TEXT REFERENCES gtfs_work.agency_catalog(agency_id) ON DELETE SET NULL,
  suggested_agency_name TEXT,
  score DOUBLE PRECISION,
  status TEXT NOT NULL DEFAULT 'pending',
  resolved_agency_id TEXT REFERENCES gtfs_work.agency_catalog(agency_id) ON DELETE SET NULL,
  resolution_note TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_gtfs_work_agency_match_requests_route_pending
  ON gtfs_work.agency_match_requests(route_id)
  WHERE status = 'pending';

INSERT INTO gtfs_work.agency_catalog (
  agency_id,
  agency_name,
  agency_name_norm,
  agency_url,
  agency_timezone,
  agency_lang,
  is_active
)
VALUES (
  'datamind',
  'DataMind Transit',
  'datamind transit',
  'https://datamind.local',
  'America/Guayaquil',
  'es',
  TRUE
)
ON CONFLICT (agency_id) DO UPDATE SET
  agency_name = EXCLUDED.agency_name,
  agency_name_norm = EXCLUDED.agency_name_norm,
  agency_url = EXCLUDED.agency_url,
  agency_timezone = EXCLUDED.agency_timezone,
  agency_lang = EXCLUDED.agency_lang,
  is_active = TRUE,
  updated_at = now();
