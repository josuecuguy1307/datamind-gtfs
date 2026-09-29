-- ============================================================
-- Phase 4-5 Automation: Catalog & Diagnostics schemas
-- ============================================================
-- Creates the catalog and automation schemas with all tables
-- needed for the Phase 4->5 automation pipeline.
--
-- Safe to re-run (IF NOT EXISTS throughout).
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS catalog;
CREATE SCHEMA IF NOT EXISTS automation;

-- ============================================================
-- 1. catalog.route_semantics
-- Phase 4 truth source. Fixes the public identity of every route.
-- ============================================================

CREATE TABLE IF NOT EXISTS catalog.route_semantics (
    route_id            UUID PRIMARY KEY
        REFERENCES route_prod.routes(route_id) ON DELETE RESTRICT,
    operator            TEXT NOT NULL,
    route_short_name    TEXT NOT NULL,
    route_long_name     TEXT NOT NULL,
    route_type          INT NOT NULL DEFAULT 3,
    public_origin       TEXT NOT NULL,
    public_destination  TEXT NOT NULL,
    aliases             TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
    description         TEXT,
    jurisdiction        TEXT NOT NULL CHECK (jurisdiction IN ('DMQ', 'ANT', 'MIXED')),
    evidence_source     TEXT NOT NULL,
    confidence          NUMERIC(3,2) CHECK (confidence BETWEEN 0.00 AND 1.00),
    approved            BOOLEAN NOT NULL DEFAULT FALSE,
    approved_by         TEXT,
    approved_at         TIMESTAMPTZ,
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Auto-update updated_at on row change.
CREATE OR REPLACE FUNCTION catalog.fn_update_timestamp()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_route_semantics_updated_at ON catalog.route_semantics;
CREATE TRIGGER trg_route_semantics_updated_at
    BEFORE UPDATE ON catalog.route_semantics
    FOR EACH ROW
    EXECUTE FUNCTION catalog.fn_update_timestamp();

-- ============================================================
-- 2. catalog.route_service_days
-- Defines when each route operates.
-- ============================================================

CREATE TABLE IF NOT EXISTS catalog.route_service_days (
    id                  SERIAL PRIMARY KEY,
    route_id            UUID NOT NULL
        REFERENCES catalog.route_semantics(route_id) ON DELETE CASCADE,
    service_pattern_id  TEXT NOT NULL,
    monday              BOOLEAN NOT NULL DEFAULT FALSE,
    tuesday             BOOLEAN NOT NULL DEFAULT FALSE,
    wednesday           BOOLEAN NOT NULL DEFAULT FALSE,
    thursday            BOOLEAN NOT NULL DEFAULT FALSE,
    friday              BOOLEAN NOT NULL DEFAULT FALSE,
    saturday            BOOLEAN NOT NULL DEFAULT FALSE,
    sunday              BOOLEAN NOT NULL DEFAULT FALSE,
    first_departure     TIME NOT NULL,
    last_departure      TIME NOT NULL,
    headway_min         INT,
    valid_from          DATE NOT NULL,
    valid_to            DATE,
    source              TEXT NOT NULL,
    confidence          NUMERIC(3,2) CHECK (confidence BETWEEN 0.00 AND 1.00),
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE(route_id, service_pattern_id),
    CHECK (first_departure < last_departure)
);

CREATE INDEX IF NOT EXISTS idx_route_service_days_route
    ON catalog.route_service_days(route_id);

-- ============================================================
-- 3. catalog.route_schedule_profile
-- Per-direction, per-window scheduling parameters.
-- ============================================================

CREATE TABLE IF NOT EXISTS catalog.route_schedule_profile (
    id                      SERIAL PRIMARY KEY,
    route_id                UUID NOT NULL
        REFERENCES catalog.route_semantics(route_id) ON DELETE CASCADE,
    direction_id            INT NOT NULL CHECK (direction_id IN (0, 1)),
    service_pattern_id      TEXT NOT NULL,
    window_start            TIME NOT NULL,
    window_end              TIME NOT NULL,
    headway_min             INT,
    exact_departures        TIME[],
    runtime_override_min    NUMERIC(5,1),
    runtime_override_reason TEXT,
    peak_type               TEXT CHECK (peak_type IN ('peak', 'offpeak', 'shoulder')),
    estimated_vehicles      INT,
    cycle_time_min          NUMERIC(5,1),
    source                  TEXT NOT NULL,
    confidence              NUMERIC(3,2) CHECK (confidence BETWEEN 0.00 AND 1.00),
    notes                   TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CHECK (window_start < window_end),
    CHECK (headway_min IS NOT NULL OR exact_departures IS NOT NULL),
    UNIQUE(route_id, direction_id, service_pattern_id, window_start)
);

CREATE INDEX IF NOT EXISTS idx_route_schedule_profile_route
    ON catalog.route_schedule_profile(route_id, direction_id);

-- ============================================================
-- 4. catalog.route_layover_policy
-- Turnaround time between directions.
-- ============================================================

CREATE TABLE IF NOT EXISTS catalog.route_layover_policy (
    id                          SERIAL PRIMARY KEY,
    route_id                    UUID NOT NULL
        REFERENCES catalog.route_semantics(route_id) ON DELETE CASCADE,
    layover_at_destination_min  NUMERIC(4,1) NOT NULL DEFAULT 5.0,
    layover_at_origin_min       NUMERIC(4,1) NOT NULL DEFAULT 5.0,
    min_layover_min             NUMERIC(4,1) NOT NULL DEFAULT 3.0,
    max_layover_min             NUMERIC(4,1) NOT NULL DEFAULT 15.0,
    applies_to_pattern          TEXT NOT NULL DEFAULT 'all',
    source                      TEXT NOT NULL,
    confidence                  NUMERIC(3,2) CHECK (confidence BETWEEN 0.00 AND 1.00),
    notes                       TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE(route_id, applies_to_pattern)
);

CREATE INDEX IF NOT EXISTS idx_route_layover_policy_route
    ON catalog.route_layover_policy(route_id);

-- ============================================================
-- 5. catalog.route_service_exceptions
-- Holidays, temporary suspensions, special operations.
-- Maps directly to calendar_dates.txt.
-- ============================================================

CREATE TABLE IF NOT EXISTS catalog.route_service_exceptions (
    id                  SERIAL PRIMARY KEY,
    route_id            UUID NOT NULL
        REFERENCES catalog.route_semantics(route_id) ON DELETE CASCADE,
    exception_date      DATE NOT NULL,
    exception_type      INT NOT NULL CHECK (exception_type IN (1, 2)),
    override_first_dep  TIME,
    override_last_dep   TIME,
    reason              TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE(route_id, exception_date)
);

CREATE INDEX IF NOT EXISTS idx_route_service_exceptions_route
    ON catalog.route_service_exceptions(route_id);

-- ============================================================
-- 6. automation.diagnostics
-- Stores every diagnostic run.
-- ============================================================

CREATE TABLE IF NOT EXISTS automation.diagnostics (
    id          SERIAL PRIMARY KEY,
    run_id      TEXT NOT NULL,
    route_id    UUID NOT NULL,
    phase       TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('CLEAN', 'WARNINGS', 'BLOCKED')),
    report      JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_diagnostics_run_route
    ON automation.diagnostics(run_id, route_id);

CREATE INDEX IF NOT EXISTS idx_diagnostics_route_phase
    ON automation.diagnostics(route_id, phase, created_at DESC);

-- ============================================================
-- 7. automation.patches
-- Logs every patch attempt.
-- ============================================================

CREATE TABLE IF NOT EXISTS automation.patches (
    id              SERIAL PRIMARY KEY,
    run_id          TEXT NOT NULL,
    route_id        UUID NOT NULL,
    failed_agent    TEXT NOT NULL,
    error_trace     TEXT NOT NULL,
    file_patched    TEXT,
    patch_diff      TEXT,
    tests_passed    BOOLEAN,
    attempt_number  INT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_patches_run_route
    ON automation.patches(run_id, route_id);
