-- Normalize naming: treat incoming operator text as incoming agency text in requests/links.
-- Non-breaking migration: keep legacy source_operator_* columns, add source_agency_* columns.

ALTER TABLE IF EXISTS gtfs_work.agency_match_requests
  ADD COLUMN IF NOT EXISTS source_agency_name TEXT;

ALTER TABLE IF EXISTS gtfs_work.agency_match_requests
  ADD COLUMN IF NOT EXISTS source_agency_norm TEXT;

UPDATE gtfs_work.agency_match_requests
SET
  source_agency_name = COALESCE(NULLIF(source_agency_name, ''), source_operator_name),
  source_agency_norm = COALESCE(NULLIF(source_agency_norm, ''), source_operator_norm)
WHERE
  COALESCE(source_agency_name, '') = ''
  OR COALESCE(source_agency_norm, '') = '';

ALTER TABLE IF EXISTS gtfs_work.route_agency_links
  ADD COLUMN IF NOT EXISTS source_agency_name TEXT;

UPDATE gtfs_work.route_agency_links
SET source_agency_name = COALESCE(NULLIF(source_agency_name, ''), source_operator_name)
WHERE COALESCE(source_agency_name, '') = '';
