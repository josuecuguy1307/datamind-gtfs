-- ============================================================
-- Phase 4-5 Automation: Catalog Approval Trigger
-- ============================================================
-- Fires pg_notify on channel 'catalog_route_approved' when
-- a route is approved in catalog.route_semantics.
--
-- The orchestrator listener (agent_orchestrator.py --listen)
-- picks up these notifications and runs the full pipeline.
--
-- Safe to re-run (CREATE OR REPLACE + DROP IF EXISTS).
-- ============================================================

-- Trigger function: fires when approved changes from FALSE to TRUE
CREATE OR REPLACE FUNCTION catalog.fn_notify_route_approved()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    -- Only fire when approved transitions FALSE → TRUE
    IF NEW.approved = TRUE AND (OLD.approved IS DISTINCT FROM TRUE) THEN
        PERFORM pg_notify(
            'catalog_route_approved',
            NEW.route_id::text
        );
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_notify_route_approved ON catalog.route_semantics;
CREATE TRIGGER trg_notify_route_approved
    AFTER UPDATE OF approved ON catalog.route_semantics
    FOR EACH ROW
    EXECUTE FUNCTION catalog.fn_notify_route_approved();

-- Also fire on INSERT with approved=TRUE (batch bootstrap case)
DROP TRIGGER IF EXISTS trg_notify_route_approved_insert ON catalog.route_semantics;
CREATE TRIGGER trg_notify_route_approved_insert
    AFTER INSERT ON catalog.route_semantics
    FOR EACH ROW
    WHEN (NEW.approved = TRUE)
    EXECUTE FUNCTION catalog.fn_notify_route_approved();

-- ============================================================
-- Convenience view: latest orchestrator run per route
-- ============================================================

CREATE OR REPLACE VIEW automation.v_latest_pipeline_status AS
SELECT DISTINCT ON (route_id)
    route_id,
    run_id,
    phase,
    status,
    report,
    created_at
FROM automation.diagnostics
WHERE phase IN ('orchestrator', 'phase5_bridge', 'phase4_execute', 'pre_execution')
ORDER BY route_id, created_at DESC;
