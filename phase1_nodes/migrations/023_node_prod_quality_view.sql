-- Quality tier view for node_prod.nodes
-- Allows downstream phases to filter by quality without deleting data.

CREATE OR REPLACE VIEW node_prod.v_nodes_quality AS
SELECT *,
    CASE
        WHEN confidence >= 0.7 THEN 'HIGH'
        WHEN confidence >= 0.4 THEN 'MEDIUM'
        ELSE 'LOW'
    END AS quality_tier
FROM node_prod.nodes;
