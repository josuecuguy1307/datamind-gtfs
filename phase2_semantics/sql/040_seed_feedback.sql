-- Block 6: Seed implicit feedback from Phase 3 approved routes.
-- Run once after Phase 3 has approved routes to bootstrap the feedback loop.

-- 6.1 Name feedback: if Phase 3 used a stop, its current name is implicitly good.
-- Join through node_place_map_work to get valid place_candidate_ids that satisfy the FK.
INSERT INTO geo_work.place_name_feedback
    (place_set_id, place_candidate_id, chosen_name, chosen_name_norm, chosen_source, created_at)
SELECT DISTINCT ON (npmw.place_candidate_id)
    npmw.place_set_id,
    npmw.place_candidate_id,
    p.canonical_name,
    lower(p.canonical_name),
    'phase3_implicit',
    now()
FROM route_prod.routes r
CROSS JOIN LATERAL unnest(r.stop_node_ids) AS sn(node_id)
JOIN geo_work.node_place_map_work npmw ON npmw.node_id = sn.node_id
JOIN geo_work.place_candidates pc ON pc.place_candidate_id = npmw.place_candidate_id
JOIN geo_prod.node_place_map npm ON npm.node_id = sn.node_id
JOIN geo_prod.places p ON p.place_id = npm.place_id
WHERE p.status = 'active'
  AND p.canonical_name IS NOT NULL
  AND p.canonical_name NOT IN ('(sin nombre)', 'SN', 'Parada', 'Parada Sin Nombre')
ON CONFLICT DO NOTHING;

-- 6.2 Type feedback: if Phase 3 used it as a route stop, confirm it's a STOP.
INSERT INTO geo_work.poi_stop_feedback
    (place_set_id, place_candidate_id, chosen_place_type, chosen_source, created_at)
SELECT DISTINCT ON (npmw.place_candidate_id)
    npmw.place_set_id,
    npmw.place_candidate_id,
    'STOP',
    'phase3_implicit',
    now()
FROM route_prod.routes r
CROSS JOIN LATERAL unnest(r.stop_node_ids) AS sn(node_id)
JOIN geo_work.node_place_map_work npmw ON npmw.node_id = sn.node_id
JOIN geo_work.place_candidates pc ON pc.place_candidate_id = npmw.place_candidate_id
WHERE npmw.place_candidate_id IN (
    SELECT place_candidate_id FROM geo_work.node_place_map_work
    WHERE node_id IN (
        SELECT node_id FROM geo_prod.node_place_map
        WHERE place_id IN (SELECT place_id FROM geo_prod.places WHERE status = 'active')
    )
)
ON CONFLICT DO NOTHING;
