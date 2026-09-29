-- Phase 4.5 (PROMETHEUS) audit table.
-- See workspace/skills/direction_construction.md.
-- Apply locally only via your migration runner. NEVER apply to AWS schema.

CREATE TABLE IF NOT EXISTS node_prod.direction_construction_audit (
  id                   BIGSERIAL PRIMARY KEY,
  run_id               UUID NOT NULL,
  route_id             UUID NOT NULL,
  prev_state           TEXT,
  new_state            TEXT NOT NULL,
  prev_direction_id    SMALLINT,
  new_direction_id     SMALLINT,
  paired_route_id      UUID,
  synthesized_node_ids UUID[],
  pair_score           REAL,
  source               TEXT NOT NULL,
  reason               TEXT,
  created_at           TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dca_run   ON node_prod.direction_construction_audit(run_id);
CREATE INDEX IF NOT EXISTS idx_dca_route ON node_prod.direction_construction_audit(route_id);
CREATE INDEX IF NOT EXISTS idx_dca_state ON node_prod.direction_construction_audit(new_state);
