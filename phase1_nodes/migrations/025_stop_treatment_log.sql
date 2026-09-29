-- Migration 025 — node_prod.stop_treatment_log
-- Foundation for the universal stop quality treater (PROMPT 3).
-- Audit table tracking every treat_stop() invocation with full cascade evidence.

BEGIN;

CREATE TABLE IF NOT EXISTS node_prod.stop_treatment_log (
    id                          BIGSERIAL PRIMARY KEY,
    treatment_id                UUID         NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    node_id                     UUID,                                -- nullable for synthetic_insert pre-INSERT
    operation                   TEXT         NOT NULL CHECK (operation IN (
        'synthetic_insert',
        'name_repair',
        'snap_align',
        'refill_adopt',
        'ground_validate',
        'cover_validate',
        'phase3_end_audit'
    )),
    caller                      TEXT         NOT NULL,
    name_before                 TEXT,
    name_after                  TEXT,
    name_was_forbidden          BOOLEAN,
    name_normalized             BOOLEAN,
    context_name_applied        BOOLEAN,
    context_name_method         TEXT,
    coord_before_lat            NUMERIC,
    coord_before_lon            NUMERIC,
    coord_after_lat             NUMERIC,
    coord_after_lon             NUMERIC,
    coord_changed               BOOLEAN,
    place_id                    UUID,
    place_mapping_created       BOOLEAN,
    place_mapping_validated     BOOLEAN,
    success                     BOOLEAN      NOT NULL,
    error_reason                TEXT,
    treated_at                  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    treated_by                  TEXT,
    transaction_id              BIGINT
);

CREATE INDEX IF NOT EXISTS ix_stop_treatment_log_node      ON node_prod.stop_treatment_log (node_id, treated_at DESC);
CREATE INDEX IF NOT EXISTS ix_stop_treatment_log_caller    ON node_prod.stop_treatment_log (caller, treated_at DESC);
CREATE INDEX IF NOT EXISTS ix_stop_treatment_log_operation ON node_prod.stop_treatment_log (operation, success);
CREATE INDEX IF NOT EXISTS ix_stop_treatment_log_failures  ON node_prod.stop_treatment_log (treated_at DESC) WHERE success = FALSE;

COMMIT;
