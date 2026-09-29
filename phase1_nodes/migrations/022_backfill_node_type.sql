-- Backfill node_type for existing node_prod.nodes
-- Previously all nodes were hardcoded as 'STOP'; this fixes POI classification.

UPDATE node_prod.nodes np
SET node_type = CASE
    WHEN nc.tag_kind = 'poi' THEN 'POI'
    ELSE 'STOP'
END
FROM node_work.node_candidates nc
JOIN node_work.nodes_resolved nr ON nr.chosen_candidate_id = nc.node_candidate_id
WHERE nr.node_id = np.node_id
  AND nc.tag_kind = 'poi'
  AND np.node_type = 'STOP';

-- Verify
-- SELECT node_type, COUNT(*) FROM node_prod.nodes GROUP BY node_type;
