CREATE TABLE IF NOT EXISTS node_prod.precision_snap_audit (
  id         BIGSERIAL       PRIMARY KEY,
  run_id     UUID            NOT NULL,
  node_id    UUID            NOT NULL,
  route_id   UUID            NOT NULL,
  direction  SMALLINT        NOT NULL,
  stop_sequence INT,
  orig_lat   DOUBLE PRECISION,
  orig_lng   DOUBLE PRECISION,
  new_lat    DOUBLE PRECISION,
  new_lng    DOUBLE PRECISION,
  shift_m    DOUBLE PRECISION,
  arc_length_s DOUBLE PRECISION,
  source_type TEXT,
  confidence REAL,
  action     TEXT NOT NULL,
  reason     TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_psa_run   ON node_prod.precision_snap_audit(run_id);
CREATE INDEX IF NOT EXISTS idx_psa_route ON node_prod.precision_snap_audit(route_id);
CREATE INDEX IF NOT EXISTS idx_psa_node  ON node_prod.precision_snap_audit(node_id);
