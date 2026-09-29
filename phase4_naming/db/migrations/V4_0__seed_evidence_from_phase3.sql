-- Seed evidence from Phase 3 chosen OSM relation tags
-- Idempotent: uses ON CONFLICT DO NOTHING because (source_type, source_id) is unique.

INSERT INTO semantics.route_evidence_records (
  source_type,
  source_id,
  route_id_hint,
  route_ref,
  route_name,
  operator_name,
  from_name,
  to_name,
  confidence_hint,
  raw
)
SELECT
  'osm_relation' AS source_type,
  s.osm_relation_id::text AS source_id,
  s.route_id AS route_id_hint,

  NULLIF(s.tags->>'ref', '') AS route_ref,
  NULLIF(s.tags->>'name', '') AS route_name,
  NULLIF(s.tags->>'operator', '') AS operator_name,
  NULLIF(s.tags->>'from', '') AS from_name,
  NULLIF(s.tags->>'to', '') AS to_name,

  0.90 AS confidence_hint,
  jsonb_build_object(
    'known_ref', s.known_ref,
    'tags', COALESCE(s.tags, '{}'::jsonb)
  ) AS raw
FROM semantics.v_phase4_seed s
WHERE s.osm_relation_id IS NOT NULL
ON CONFLICT (source_type, source_id) DO NOTHING;
