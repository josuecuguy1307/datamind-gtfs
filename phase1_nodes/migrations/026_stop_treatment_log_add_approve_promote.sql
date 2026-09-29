-- Migration 026 — extend stop_treatment_log.operation CHECK constraint
-- Adds 'approve_promote' to the allowed operations. The treater introduced
-- this operation to handle operator-approval UPSERT paths in
-- datamind_console/phases/phase1_nodes/client.py (approve_resolved_node,
-- approve_node_review_request, create_prod_node_manual).

BEGIN;

ALTER TABLE node_prod.stop_treatment_log
    DROP CONSTRAINT IF EXISTS stop_treatment_log_operation_check;

ALTER TABLE node_prod.stop_treatment_log
    ADD CONSTRAINT stop_treatment_log_operation_check
    CHECK (operation IN (
        'synthetic_insert',
        'name_repair',
        'snap_align',
        'refill_adopt',
        'ground_validate',
        'cover_validate',
        'phase3_end_audit',
        'approve_promote'
    ));

COMMIT;
