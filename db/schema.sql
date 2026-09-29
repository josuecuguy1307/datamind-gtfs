-- =====================================================================
--  DataMind GTFS — esquema completo de la base (SOLO estructura, sin datos)
--
--  Generado con pg_dump --schema-only y saneado (sin propietarios, permisos,
--  ni datos; sin tablas de respaldo puntuales). Requiere PostgreSQL 15+ con las
--  extensiones: postgis, vector (pgvector), pgcrypto, pg_trgm, citext,
--  unaccent y dblink. Se aplica con:   python scripts/init_db.py
--
--  Parámetro opcional `datamind.audit_dsn`: DSN de dblink con el que el trigger
--  route_prod.trg_routes_enforce_audit_trail registra los rechazos fuera de la
--  transacción. Si no lo defines, el rechazo se aplica igual, sin ese registro.
-- =====================================================================

--
-- PostgreSQL database dump
--



SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: ai; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA ai;


--
-- Name: automation; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA automation;


--
-- Name: catalog; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA catalog;


--
-- Name: console; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA console;


--
-- Name: geo_prod; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA geo_prod;


--
-- Name: geo_raw; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA geo_raw;


--
-- Name: geo_work; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA geo_work;


--
-- Name: gtfs; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA gtfs;


--
-- Name: gtfs_prod; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA gtfs_prod;


--
-- Name: gtfs_work; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA gtfs_work;


--
-- Name: node_prod; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA node_prod;


--
-- Name: node_raw; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA node_raw;


--
-- Name: node_work; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA node_work;


--
-- Name: route_prod; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA route_prod;


--
-- Name: route_raw; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA route_raw;


--
-- Name: route_review; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA route_review;


--
-- Name: route_trash; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA route_trash;


--
-- Name: route_work; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA route_work;


--
-- Name: semantics; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA semantics;


--
-- Name: fn_notify_route_approved(); Type: FUNCTION; Schema: catalog; Owner: -
--

CREATE FUNCTION catalog.fn_notify_route_approved() RETURNS trigger
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


--
-- Name: fn_update_timestamp(); Type: FUNCTION; Schema: catalog; Owner: -
--

CREATE FUNCTION catalog.fn_update_timestamp() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;


--
-- Name: set_updated_at(); Type: FUNCTION; Schema: console; Owner: -
--

CREATE FUNCTION console.set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$;


--
-- Name: capture_place_history(); Type: FUNCTION; Schema: geo_prod; Owner: -
--

CREATE FUNCTION geo_prod.capture_place_history() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF OLD.canonical_name IS DISTINCT FROM NEW.canonical_name
       OR OLD.place_type IS DISTINCT FROM NEW.place_type
       OR OLD.status IS DISTINCT FROM NEW.status THEN
        INSERT INTO geo_prod.place_history
            (place_id, previous_canonical_name, previous_place_type,
             previous_status, previous_geom, changed_by, change_reason)
        VALUES
            (OLD.place_id, OLD.canonical_name, OLD.place_type,
             OLD.status, OLD.geom,
             COALESCE(current_setting('app.changed_by', true), 'unknown'),
             current_setting('app.change_reason', true));
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: normalize_alias(text); Type: FUNCTION; Schema: geo_work; Owner: -
--

CREATE FUNCTION geo_work.normalize_alias(s text) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $$
  SELECT
    regexp_replace(
      regexp_replace(
        lower(unaccent(coalesce(s,''))),
        '[^a-z0-9\s]+', ' ', 'g'
      ),
      '\s+', ' ', 'g'
    )::text
$$;


--
-- Name: fn_sync_gtfs_id_from_export_run(); Type: FUNCTION; Schema: gtfs_work; Owner: -
--

CREATE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
  v_gtfs_id text;
BEGIN
  IF NEW.gtfs_id IS NULL OR NEW.gtfs_id = '' THEN
    SELECT e.gtfs_id INTO v_gtfs_id
    FROM gtfs_work.export_runs e
    WHERE e.export_run_id = NEW.export_run_id
    LIMIT 1;
    NEW.gtfs_id := COALESCE(v_gtfs_id, NEW.gtfs_id);
  END IF;
  RETURN NEW;
END;
$$;


--
-- Name: route_prod_writer_sp(uuid, public.geometry, uuid[], text, text, text, text, jsonb, jsonb, jsonb, timestamp with time zone, uuid, timestamp with time zone, text, smallint); Type: FUNCTION; Schema: route_prod; Owner: -
--

CREATE FUNCTION route_prod.route_prod_writer_sp(p_route_id uuid, p_geom public.geometry, p_stop_node_ids uuid[], p_source text, p_source_type text, p_province text, p_pipeline_version text, p_valhalla_request jsonb, p_geometry_enforcer_report jsonb, p_stop_coverage_report jsonb, p_quality_gate_passed_at timestamp with time zone, p_approved_by_user uuid DEFAULT NULL::uuid, p_approved_at timestamp with time zone DEFAULT NULL::timestamp with time zone, p_route_name text DEFAULT NULL::text, p_version smallint DEFAULT (1)::smallint) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'route_prod', 'public'
    AS $$
BEGIN
  -- SP-level validation (defense in depth; trigger + CHECK are the real gates).
  IF p_pipeline_version IS NULL THEN
    RAISE EXCEPTION 'route_prod_writer_sp: p_pipeline_version is required';
  END IF;
  IF p_valhalla_request IS NULL THEN
    RAISE EXCEPTION 'route_prod_writer_sp: p_valhalla_request is required';
  END IF;
  IF p_geometry_enforcer_report IS NULL THEN
    RAISE EXCEPTION 'route_prod_writer_sp: p_geometry_enforcer_report is required';
  END IF;
  IF p_stop_coverage_report IS NULL THEN
    RAISE EXCEPTION 'route_prod_writer_sp: p_stop_coverage_report is required';
  END IF;
  IF p_quality_gate_passed_at IS NULL THEN
    RAISE EXCEPTION 'route_prod_writer_sp: p_quality_gate_passed_at is required';
  END IF;
  IF p_source_type NOT IN (
    'constructor_canonical','osm_relation_import','manual_constructor',
    'discovery_pipeline','deep_research_override','synthetic_fill'
  ) THEN
    RAISE EXCEPTION 'route_prod_writer_sp: invalid p_source_type %', p_source_type;
  END IF;

  INSERT INTO route_prod.routes (
    route_id, version, geom, stop_node_ids, source, source_type, province,
    pipeline_version, valhalla_request, geometry_enforcer_report,
    stop_coverage_report, quality_gate_passed_at, approved_by_user, approved_at,
    route_name, legacy_grandfathered, grandfathered_until,
    route_aliases, landmark_tags, direction_semantics,
    naming_confidence, human_verified, semantics_updated_at,
    created_at, updated_at, deploy_status, pending_human_review
  ) VALUES (
    p_route_id, p_version, p_geom, p_stop_node_ids, p_source, p_source_type, p_province,
    p_pipeline_version, p_valhalla_request, p_geometry_enforcer_report,
    p_stop_coverage_report, p_quality_gate_passed_at, p_approved_by_user, p_approved_at,
    p_route_name, FALSE, NULL,
    ARRAY[]::TEXT[], ARRAY[]::TEXT[], '{}'::JSONB,
    0.0, FALSE, NOW(),
    NOW(), NOW(), 'canary', FALSE
  )
  ON CONFLICT (route_id, version) DO UPDATE SET
    geom                     = EXCLUDED.geom,
    stop_node_ids            = EXCLUDED.stop_node_ids,
    source                   = EXCLUDED.source,
    source_type              = EXCLUDED.source_type,
    province                 = EXCLUDED.province,
    pipeline_version         = EXCLUDED.pipeline_version,
    valhalla_request         = EXCLUDED.valhalla_request,
    geometry_enforcer_report = EXCLUDED.geometry_enforcer_report,
    stop_coverage_report     = EXCLUDED.stop_coverage_report,
    quality_gate_passed_at   = EXCLUDED.quality_gate_passed_at,
    approved_by_user         = EXCLUDED.approved_by_user,
    approved_at              = EXCLUDED.approved_at,
    route_name               = COALESCE(EXCLUDED.route_name, route_prod.routes.route_name),
    updated_at               = NOW();

  RETURN p_route_id;
END;
$$;


--
-- Name: touch_cleanliness_classified_at(); Type: FUNCTION; Schema: route_prod; Owner: -
--

CREATE FUNCTION route_prod.touch_cleanliness_classified_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  IF (NEW.cleanliness_status IS DISTINCT FROM OLD.cleanliness_status)
     OR (NEW.dirty_reason   IS DISTINCT FROM OLD.dirty_reason) THEN
    NEW.cleanliness_classified_at = NOW();
  END IF;
  RETURN NEW;
END;
$$;


--
-- Name: touch_updated_at(); Type: FUNCTION; Schema: route_prod; Owner: -
--

CREATE FUNCTION route_prod.touch_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;


--
-- Name: trg_routes_enforce_audit_trail(); Type: FUNCTION; Schema: route_prod; Owner: -
--

CREATE FUNCTION route_prod.trg_routes_enforce_audit_trail() RETURNS trigger
    LANGUAGE plpgsql
    AS $_$
DECLARE
  v_err   TEXT;
  v_app   TEXT;
  v_user  TEXT;
  v_sql   TEXT;
  -- DSN (dblink) para registrar el rechazo fuera de la transacción. NO va incrustado:
  -- se toma del parámetro `datamind.audit_dsn` (ver README, sección Base de datos).
  v_dsn   TEXT := current_setting('datamind.audit_dsn', true);
BEGIN
  v_app  := COALESCE(current_setting('application_name', true), '');
  v_user := current_user::text;

  IF COALESCE(NEW.legacy_grandfathered, FALSE) THEN
    IF NEW.grandfathered_until IS NULL THEN
      v_err := 'grandfathered row missing grandfathered_until';
    ELSIF NEW.grandfathered_until <= NOW() THEN
      v_err := format('grandfathering deadline expired at %s', NEW.grandfathered_until);
    END IF;
  ELSE
    IF NEW.pipeline_version IS NULL THEN
      v_err := 'pipeline_version is NULL for non-grandfathered route';
    ELSIF NEW.valhalla_request IS NULL THEN
      v_err := 'valhalla_request is NULL for non-grandfathered route';
    ELSIF NEW.geometry_enforcer_report IS NULL THEN
      v_err := 'geometry_enforcer_report is NULL for non-grandfathered route';
    ELSIF NEW.stop_coverage_report IS NULL THEN
      v_err := 'stop_coverage_report is NULL for non-grandfathered route';
    ELSIF NEW.quality_gate_passed_at IS NULL THEN
      v_err := 'quality_gate_passed_at is NULL for non-grandfathered route';
    END IF;
  END IF;

  IF v_err IS NOT NULL THEN
    -- Autonomous-style reject log via dblink so the entry survives the RAISE.
    v_sql := format(
      $sql$INSERT INTO route_prod.routes_audit
             (route_id, version, action, approval_reason, trigger_function, application_name, "session_user")
           VALUES (%L::uuid, %L::smallint, %L, %L, %L, %L, %L)$sql$,
      NEW.route_id, NEW.version, 'REJECT', v_err,
      'trg_routes_enforce_audit_trail', v_app, v_user
    );
    IF COALESCE(v_dsn, '') <> '' THEN
      PERFORM dblink_exec(v_dsn, v_sql);
    END IF;

    RAISE EXCEPTION 'route_prod.routes write rejected: %', v_err;
  END IF;

  -- Accept path: ordinary INSERT; commits with the outer transaction.
  INSERT INTO route_prod.routes_audit (
    route_id, version, action, approved_by, trigger_function, application_name, "session_user"
  ) VALUES (
    NEW.route_id, NEW.version, TG_OP, NEW.approved_by_user,
    'trg_routes_enforce_audit_trail', v_app, v_user
  );

  RETURN NEW;
END;
$_$;


--
-- Name: touch_updated_at(); Type: FUNCTION; Schema: route_review; Owner: -
--

CREATE FUNCTION route_review.touch_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;


--
-- Name: touch_inverse_direction_status_updated_at(); Type: FUNCTION; Schema: route_work; Owner: -
--

CREATE FUNCTION route_work.touch_inverse_direction_status_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;


--
-- Name: touch_sequence_approval_updated_at(); Type: FUNCTION; Schema: route_work; Owner: -
--

CREATE FUNCTION route_work.touch_sequence_approval_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;


--
-- Name: route_semantics_search_tsv_sync(); Type: FUNCTION; Schema: semantics; Owner: -
--

CREATE FUNCTION semantics.route_semantics_search_tsv_sync() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  NEW.search_tsv :=
    to_tsvector(
      'simple'::regconfig,
      concat_ws(' ',
        COALESCE(NEW.route_name, ''),
        array_to_string(COALESCE(NEW.route_aliases, ARRAY[]::text[]), ' '),
        array_to_string(COALESCE(NEW.landmark_tags, ARRAY[]::text[]), ' ')
      )
    );
  NEW.semantics_updated_at := now();
  RETURN NEW;
END;
$$;


--
-- Name: sync_route_semantics_direction_context(); Type: FUNCTION; Schema: semantics; Owner: -
--

CREATE FUNCTION semantics.sync_route_semantics_direction_context() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  SELECT r.service_route_id, r.direction_id
  INTO NEW.service_route_id, NEW.direction_id
  FROM route_prod.routes r
  WHERE r.route_id = NEW.route_id;
  RETURN NEW;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: ai_agent_schedule; Type: TABLE; Schema: ai; Owner: -
--

CREATE TABLE ai.ai_agent_schedule (
    schedule_id integer DEFAULT 1 NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    timezone text DEFAULT 'America/Guayaquil'::text NOT NULL,
    days_of_week integer[] DEFAULT ARRAY[1, 2, 3, 4, 5, 6, 7] NOT NULL,
    start_time_local time without time zone DEFAULT '09:00:00'::time without time zone NOT NULL,
    end_time_local time without time zone DEFAULT '18:00:00'::time without time zone NOT NULL,
    interval_seconds integer DEFAULT 600 NOT NULL,
    run_once_now boolean DEFAULT false NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by text,
    CONSTRAINT chk_ai_agent_schedule_days CHECK (((array_length(days_of_week, 1) >= 1) AND (days_of_week <@ ARRAY[1, 2, 3, 4, 5, 6, 7]))),
    CONSTRAINT chk_ai_agent_schedule_interval CHECK (((interval_seconds >= 60) AND (interval_seconds <= 86400))),
    CONSTRAINT chk_ai_agent_schedule_window CHECK ((end_time_local > start_time_local))
);


--
-- Name: ai_bot_model_metrics; Type: TABLE; Schema: ai; Owner: -
--

CREATE TABLE ai.ai_bot_model_metrics (
    metric_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    task text NOT NULL,
    event_type text DEFAULT 'eval'::text NOT NULL,
    status text DEFAULT 'success'::text NOT NULL,
    ok boolean DEFAULT true NOT NULL,
    metric_primary_name text,
    metric_primary_value double precision,
    metric_higher_better boolean,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT ai_bot_model_metrics_event_type_check CHECK ((event_type = ANY (ARRAY['train'::text, 'eval'::text, 'manual'::text, 'system'::text]))),
    CONSTRAINT ai_bot_model_metrics_status_check CHECK ((status = ANY (ARRAY['success'::text, 'partial'::text, 'failed'::text])))
);


--
-- Name: ai_bot_model_metrics_metric_id_seq; Type: SEQUENCE; Schema: ai; Owner: -
--

CREATE SEQUENCE ai.ai_bot_model_metrics_metric_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ai_bot_model_metrics_metric_id_seq; Type: SEQUENCE OWNED BY; Schema: ai; Owner: -
--

ALTER SEQUENCE ai.ai_bot_model_metrics_metric_id_seq OWNED BY ai.ai_bot_model_metrics.metric_id;


--
-- Name: ai_bot_run_logs; Type: TABLE; Schema: ai; Owner: -
--

CREATE TABLE ai.ai_bot_run_logs (
    log_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    phase text NOT NULL,
    stage text,
    event_type text DEFAULT 'run'::text NOT NULL,
    status text DEFAULT 'success'::text NOT NULL,
    run_id text,
    node_set_id text,
    route_id text,
    service_route_id text,
    direction_id integer,
    quality_score double precision,
    sequence_quality_score double precision,
    reorder_recommended boolean,
    reorder_confidence double precision,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    warnings text[] DEFAULT ARRAY[]::text[] NOT NULL,
    notes text[] DEFAULT ARRAY[]::text[] NOT NULL,
    CONSTRAINT ai_bot_run_logs_event_type_check CHECK ((event_type = ANY (ARRAY['run'::text, 'sequence_edit'::text, 'system'::text]))),
    CONSTRAINT ai_bot_run_logs_phase_check CHECK ((phase = ANY (ARRAY['phase1'::text, 'phase3'::text]))),
    CONSTRAINT ai_bot_run_logs_reorder_confidence_check CHECK (((reorder_confidence IS NULL) OR ((reorder_confidence >= (0.0)::double precision) AND (reorder_confidence <= (1.0)::double precision)))),
    CONSTRAINT ai_bot_run_logs_status_check CHECK ((status = ANY (ARRAY['success'::text, 'partial'::text, 'failed'::text])))
);


--
-- Name: ai_bot_run_logs_log_id_seq; Type: SEQUENCE; Schema: ai; Owner: -
--

CREATE SEQUENCE ai.ai_bot_run_logs_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ai_bot_run_logs_log_id_seq; Type: SEQUENCE OWNED BY; Schema: ai; Owner: -
--

ALTER SEQUENCE ai.ai_bot_run_logs_log_id_seq OWNED BY ai.ai_bot_run_logs.log_id;


--
-- Name: ai_bot_train_events; Type: TABLE; Schema: ai; Owner: -
--

CREATE TABLE ai.ai_bot_train_events (
    event_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    task text NOT NULL,
    event_type text DEFAULT 'manual'::text NOT NULL,
    status text DEFAULT 'success'::text NOT NULL,
    ok boolean DEFAULT true NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT ai_bot_train_events_event_type_check CHECK ((event_type = ANY (ARRAY['train'::text, 'eval'::text, 'manual'::text, 'system'::text]))),
    CONSTRAINT ai_bot_train_events_status_check CHECK ((status = ANY (ARRAY['success'::text, 'partial'::text, 'failed'::text])))
);


--
-- Name: ai_bot_train_events_event_id_seq; Type: SEQUENCE; Schema: ai; Owner: -
--

CREATE SEQUENCE ai.ai_bot_train_events_event_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ai_bot_train_events_event_id_seq; Type: SEQUENCE OWNED BY; Schema: ai; Owner: -
--

ALTER SEQUENCE ai.ai_bot_train_events_event_id_seq OWNED BY ai.ai_bot_train_events.event_id;


--
-- Name: ai_escalations; Type: TABLE; Schema: ai; Owner: -
--

CREATE TABLE ai.ai_escalations (
    escalation_id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    status text DEFAULT 'open'::text NOT NULL,
    error_type text NOT NULL,
    error_message text NOT NULL,
    stacktrace text NOT NULL,
    context jsonb DEFAULT '{}'::jsonb NOT NULL,
    codex_request jsonb,
    codex_response jsonb,
    resolution_notes text,
    CONSTRAINT ai_escalations_status_check CHECK ((status = ANY (ARRAY['open'::text, 'sent'::text, 'resolved'::text, 'failed'::text])))
);


--
-- Name: ai_label_events; Type: TABLE; Schema: ai; Owner: -
--

CREATE TABLE ai.ai_label_events (
    label_event_id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    suggestion_id uuid NOT NULL,
    phase text NOT NULL,
    entity_type text NOT NULL,
    entity_id text NOT NULL,
    human_label text NOT NULL,
    actor text NOT NULL,
    notes text,
    ui_context jsonb DEFAULT '{}'::jsonb,
    CONSTRAINT ai_label_events_human_label_check CHECK ((human_label = ANY (ARRAY['approve'::text, 'reject'::text, 'promote'::text, 'dismiss'::text, 'reviewed'::text])))
);


--
-- Name: ai_model_registry; Type: TABLE; Schema: ai; Owner: -
--

CREATE TABLE ai.ai_model_registry (
    model_id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    phase text,
    model_name text NOT NULL,
    model_version text NOT NULL,
    artifact_path text NOT NULL,
    metrics jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_active boolean DEFAULT false NOT NULL,
    trained_on jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: ai_suggestions; Type: TABLE; Schema: ai; Owner: -
--

CREATE TABLE ai.ai_suggestions (
    suggestion_id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    phase text NOT NULL,
    entity_type text NOT NULL,
    entity_id text NOT NULL,
    suggestion_type text NOT NULL,
    confidence double precision NOT NULL,
    reason text DEFAULT ''::text NOT NULL,
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL,
    features jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'open'::text NOT NULL,
    model_name text,
    model_version text,
    prediction jsonb,
    CONSTRAINT ai_suggestions_confidence_check CHECK (((confidence >= (0.0)::double precision) AND (confidence <= (1.0)::double precision))),
    CONSTRAINT ai_suggestions_status_check CHECK ((status = ANY (ARRAY['open'::text, 'reviewed'::text, 'applied'::text, 'dismissed'::text]))),
    CONSTRAINT ai_suggestions_suggestion_type_check CHECK ((suggestion_type = ANY (ARRAY['approve'::text, 'reject'::text, 'promote'::text, 'review'::text, 'investigate'::text]))),
    CONSTRAINT chk_ai_suggestions_model_pair CHECK ((((model_name IS NULL) AND (model_version IS NULL)) OR ((model_name IS NOT NULL) AND (model_version IS NOT NULL))))
);


--
-- Name: ai_training_dataset; Type: TABLE; Schema: ai; Owner: -
--

CREATE TABLE ai.ai_training_dataset (
    row_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    phase text NOT NULL,
    entity_type text NOT NULL,
    entity_id text NOT NULL,
    features jsonb DEFAULT '{}'::jsonb NOT NULL,
    label text NOT NULL,
    source_suggestion_id uuid,
    CONSTRAINT ai_training_dataset_label_check CHECK ((label = ANY (ARRAY['approve'::text, 'reject'::text, 'promote'::text, 'dismiss'::text, 'reviewed'::text])))
);


--
-- Name: ai_training_dataset_row_id_seq; Type: SEQUENCE; Schema: ai; Owner: -
--

CREATE SEQUENCE ai.ai_training_dataset_row_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ai_training_dataset_row_id_seq; Type: SEQUENCE OWNED BY; Schema: ai; Owner: -
--

ALTER SEQUENCE ai.ai_training_dataset_row_id_seq OWNED BY ai.ai_training_dataset.row_id;


--
-- Name: diagnostics; Type: TABLE; Schema: automation; Owner: -
--

CREATE TABLE automation.diagnostics (
    id integer NOT NULL,
    run_id text NOT NULL,
    route_id uuid NOT NULL,
    phase text NOT NULL,
    status text NOT NULL,
    report jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT diagnostics_status_check CHECK ((status = ANY (ARRAY['CLEAN'::text, 'WARNINGS'::text, 'BLOCKED'::text])))
);


--
-- Name: diagnostics_id_seq; Type: SEQUENCE; Schema: automation; Owner: -
--

CREATE SEQUENCE automation.diagnostics_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: diagnostics_id_seq; Type: SEQUENCE OWNED BY; Schema: automation; Owner: -
--

ALTER SEQUENCE automation.diagnostics_id_seq OWNED BY automation.diagnostics.id;


--
-- Name: patches; Type: TABLE; Schema: automation; Owner: -
--

CREATE TABLE automation.patches (
    id integer NOT NULL,
    run_id text NOT NULL,
    route_id uuid NOT NULL,
    failed_agent text NOT NULL,
    error_trace text NOT NULL,
    file_patched text,
    patch_diff text,
    tests_passed boolean,
    attempt_number integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: patches_id_seq; Type: SEQUENCE; Schema: automation; Owner: -
--

CREATE SEQUENCE automation.patches_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: patches_id_seq; Type: SEQUENCE OWNED BY; Schema: automation; Owner: -
--

ALTER SEQUENCE automation.patches_id_seq OWNED BY automation.patches.id;


--
-- Name: v_latest_pipeline_status; Type: VIEW; Schema: automation; Owner: -
--

CREATE VIEW automation.v_latest_pipeline_status AS
 SELECT DISTINCT ON (route_id) route_id,
    run_id,
    phase,
    status,
    report,
    created_at
   FROM automation.diagnostics
  WHERE (phase = ANY (ARRAY['orchestrator'::text, 'phase5_bridge'::text, 'phase4_execute'::text, 'pre_execution'::text]))
  ORDER BY route_id, created_at DESC;


--
-- Name: route_layover_policy; Type: TABLE; Schema: catalog; Owner: -
--

CREATE TABLE catalog.route_layover_policy (
    id integer NOT NULL,
    route_id uuid NOT NULL,
    layover_at_destination_min numeric(4,1) DEFAULT 5.0 NOT NULL,
    layover_at_origin_min numeric(4,1) DEFAULT 5.0 NOT NULL,
    min_layover_min numeric(4,1) DEFAULT 3.0 NOT NULL,
    max_layover_min numeric(4,1) DEFAULT 15.0 NOT NULL,
    applies_to_pattern text DEFAULT 'all'::text NOT NULL,
    source text NOT NULL,
    confidence numeric(3,2),
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT route_layover_policy_confidence_check CHECK (((confidence >= 0.00) AND (confidence <= 1.00)))
);


--
-- Name: route_layover_policy_id_seq; Type: SEQUENCE; Schema: catalog; Owner: -
--

CREATE SEQUENCE catalog.route_layover_policy_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: route_layover_policy_id_seq; Type: SEQUENCE OWNED BY; Schema: catalog; Owner: -
--

ALTER SEQUENCE catalog.route_layover_policy_id_seq OWNED BY catalog.route_layover_policy.id;


--
-- Name: route_schedule_profile; Type: TABLE; Schema: catalog; Owner: -
--

CREATE TABLE catalog.route_schedule_profile (
    id integer NOT NULL,
    route_id uuid NOT NULL,
    direction_id integer NOT NULL,
    service_pattern_id text NOT NULL,
    window_start time without time zone NOT NULL,
    window_end time without time zone NOT NULL,
    headway_min integer,
    exact_departures time without time zone[],
    runtime_override_min numeric(5,1),
    runtime_override_reason text,
    peak_type text,
    estimated_vehicles integer,
    cycle_time_min numeric(5,1),
    source text NOT NULL,
    confidence numeric(3,2),
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT route_schedule_profile_check CHECK ((window_start < window_end)),
    CONSTRAINT route_schedule_profile_check1 CHECK (((headway_min IS NOT NULL) OR (exact_departures IS NOT NULL))),
    CONSTRAINT route_schedule_profile_confidence_check CHECK (((confidence >= 0.00) AND (confidence <= 1.00))),
    CONSTRAINT route_schedule_profile_direction_id_check CHECK ((direction_id = ANY (ARRAY[0, 1]))),
    CONSTRAINT route_schedule_profile_peak_type_check CHECK ((peak_type = ANY (ARRAY['peak'::text, 'offpeak'::text, 'shoulder'::text])))
);


--
-- Name: route_schedule_profile_id_seq; Type: SEQUENCE; Schema: catalog; Owner: -
--

CREATE SEQUENCE catalog.route_schedule_profile_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: route_schedule_profile_id_seq; Type: SEQUENCE OWNED BY; Schema: catalog; Owner: -
--

ALTER SEQUENCE catalog.route_schedule_profile_id_seq OWNED BY catalog.route_schedule_profile.id;


--
-- Name: route_semantics; Type: TABLE; Schema: catalog; Owner: -
--

CREATE TABLE catalog.route_semantics (
    route_id uuid NOT NULL,
    operator text NOT NULL,
    route_short_name text NOT NULL,
    route_long_name text NOT NULL,
    route_type integer DEFAULT 3 NOT NULL,
    public_origin text NOT NULL,
    public_destination text NOT NULL,
    aliases text[] DEFAULT ARRAY[]::text[] NOT NULL,
    description text,
    jurisdiction text NOT NULL,
    evidence_source text NOT NULL,
    confidence numeric(3,2),
    approved boolean DEFAULT false NOT NULL,
    approved_by text,
    approved_at timestamp with time zone,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT route_semantics_confidence_check CHECK (((confidence >= 0.00) AND (confidence <= 1.00))),
    CONSTRAINT route_semantics_jurisdiction_nonempty CHECK ((length(TRIM(BOTH FROM jurisdiction)) > 0))
);


--
-- Name: route_service_days; Type: TABLE; Schema: catalog; Owner: -
--

CREATE TABLE catalog.route_service_days (
    id integer NOT NULL,
    route_id uuid NOT NULL,
    service_pattern_id text NOT NULL,
    monday boolean DEFAULT false NOT NULL,
    tuesday boolean DEFAULT false NOT NULL,
    wednesday boolean DEFAULT false NOT NULL,
    thursday boolean DEFAULT false NOT NULL,
    friday boolean DEFAULT false NOT NULL,
    saturday boolean DEFAULT false NOT NULL,
    sunday boolean DEFAULT false NOT NULL,
    first_departure time without time zone NOT NULL,
    last_departure time without time zone NOT NULL,
    headway_min integer,
    valid_from date NOT NULL,
    valid_to date,
    source text NOT NULL,
    confidence numeric(3,2),
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT route_service_days_check CHECK ((first_departure < last_departure)),
    CONSTRAINT route_service_days_confidence_check CHECK (((confidence >= 0.00) AND (confidence <= 1.00)))
);


--
-- Name: route_service_days_id_seq; Type: SEQUENCE; Schema: catalog; Owner: -
--

CREATE SEQUENCE catalog.route_service_days_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: route_service_days_id_seq; Type: SEQUENCE OWNED BY; Schema: catalog; Owner: -
--

ALTER SEQUENCE catalog.route_service_days_id_seq OWNED BY catalog.route_service_days.id;


--
-- Name: route_service_exceptions; Type: TABLE; Schema: catalog; Owner: -
--

CREATE TABLE catalog.route_service_exceptions (
    id integer NOT NULL,
    route_id uuid NOT NULL,
    exception_date date NOT NULL,
    exception_type integer NOT NULL,
    override_first_dep time without time zone,
    override_last_dep time without time zone,
    reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT route_service_exceptions_exception_type_check CHECK ((exception_type = ANY (ARRAY[1, 2])))
);


--
-- Name: route_service_exceptions_id_seq; Type: SEQUENCE; Schema: catalog; Owner: -
--

CREATE SEQUENCE catalog.route_service_exceptions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: route_service_exceptions_id_seq; Type: SEQUENCE OWNED BY; Schema: catalog; Owner: -
--

ALTER SEQUENCE catalog.route_service_exceptions_id_seq OWNED BY catalog.route_service_exceptions.id;


--
-- Name: api_keys; Type: TABLE; Schema: console; Owner: -
--

CREATE TABLE console.api_keys (
    key_id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    key_prefix text NOT NULL,
    key_hash text NOT NULL,
    label text,
    scopes text[] DEFAULT '{}'::text[] NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone,
    revoked_at timestamp with time zone,
    expires_at timestamp with time zone
);


--
-- Name: approval_queue; Type: TABLE; Schema: console; Owner: -
--

CREATE TABLE console.approval_queue (
    approval_id uuid DEFAULT gen_random_uuid() NOT NULL,
    step_id uuid NOT NULL,
    session_id uuid NOT NULL,
    action text,
    reason text,
    decided_by uuid,
    decided_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT chk_approval_action CHECK (((action IS NULL) OR (action = ANY (ARRAY['approve'::text, 'reject'::text, 'defer'::text]))))
);


--
-- Name: audit_events; Type: TABLE; Schema: console; Owner: -
--

CREATE TABLE console.audit_events (
    event_id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid,
    phase smallint,
    action text NOT NULL,
    item_type text,
    item_id text,
    candidate_id text,
    ok boolean DEFAULT true NOT NULL,
    error_message text,
    ip inet,
    user_agent text,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT chk_console_audit_phase CHECK (((phase IS NULL) OR ((phase >= 1) AND (phase <= 4))))
);


--
-- Name: email_outbox; Type: TABLE; Schema: console; Owner: -
--

CREATE TABLE console.email_outbox (
    email_id uuid DEFAULT gen_random_uuid() NOT NULL,
    to_email public.citext NOT NULL,
    subject text NOT NULL,
    body text NOT NULL,
    provider text DEFAULT 'mock'::text NOT NULL,
    status text DEFAULT 'queued'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    sent_at timestamp with time zone,
    error_message text,
    CONSTRAINT chk_console_email_outbox_status CHECK ((status = ANY (ARRAY['queued'::text, 'sent'::text, 'error'::text])))
);


--
-- Name: email_verification_tokens; Type: TABLE; Schema: console; Owner: -
--

CREATE TABLE console.email_verification_tokens (
    token_id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    email public.citext NOT NULL,
    token text NOT NULL,
    requested_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    CONSTRAINT chk_console_email_verification_expiry CHECK ((expires_at > created_at))
);


--
-- Name: export_jobs; Type: TABLE; Schema: console; Owner: -
--

CREATE TABLE console.export_jobs (
    job_id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid,
    phase smallint,
    item_id text,
    job_type text NOT NULL,
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'queued'::text NOT NULL,
    result_path text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    error_message text,
    CONSTRAINT chk_console_export_phase CHECK (((phase IS NULL) OR ((phase >= 1) AND (phase <= 4)))),
    CONSTRAINT chk_console_export_status CHECK ((status = ANY (ARRAY['queued'::text, 'running'::text, 'done'::text, 'error'::text])))
);


--
-- Name: orchestrator_sessions; Type: TABLE; Schema: console; Owner: -
--

CREATE TABLE console.orchestrator_sessions (
    session_id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid,
    profile text DEFAULT 'balanced'::text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    current_step_idx integer DEFAULT 0 NOT NULL,
    dry_run boolean DEFAULT false NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    started_at timestamp with time zone,
    ended_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT chk_orch_session_profile CHECK ((profile = ANY (ARRAY['cautious'::text, 'balanced'::text, 'aggressive'::text]))),
    CONSTRAINT chk_orch_session_status CHECK ((status = ANY (ARRAY['pending'::text, 'running'::text, 'paused'::text, 'completed'::text, 'cancelled'::text, 'failed'::text])))
);


--
-- Name: orchestrator_steps; Type: TABLE; Schema: console; Owner: -
--

CREATE TABLE console.orchestrator_steps (
    step_id uuid DEFAULT gen_random_uuid() NOT NULL,
    session_id uuid NOT NULL,
    step_key text NOT NULL,
    step_idx integer NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    policy text DEFAULT 'gate'::text NOT NULL,
    started_at timestamp with time zone,
    ended_at timestamp with time zone,
    output jsonb DEFAULT '{}'::jsonb,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT chk_orch_step_policy CHECK ((policy = ANY (ARRAY['auto'::text, 'gate'::text, 'skip'::text]))),
    CONSTRAINT chk_orch_step_status CHECK ((status = ANY (ARRAY['pending'::text, 'running'::text, 'auto_approved'::text, 'waiting_approval'::text, 'approved'::text, 'rejected'::text, 'skipped'::text, 'completed'::text, 'failed'::text])))
);


--
-- Name: phase_decisions; Type: TABLE; Schema: console; Owner: -
--

CREATE TABLE console.phase_decisions (
    decision_id uuid DEFAULT gen_random_uuid() NOT NULL,
    phase smallint NOT NULL,
    item_id text NOT NULL,
    candidate_id text,
    score_at_decision double precision,
    decision text NOT NULL,
    reason_code text,
    notes text,
    user_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT chk_console_phase_decision_value CHECK ((decision = ANY (ARRAY['APPROVE'::text, 'REJECT'::text, 'EDIT'::text, 'PUBLISH'::text]))),
    CONSTRAINT chk_console_phase_decisions_phase CHECK (((phase >= 1) AND (phase <= 4))),
    CONSTRAINT chk_reason_required_for_reject_edit CHECK ((((decision = ANY (ARRAY['REJECT'::text, 'EDIT'::text])) AND (reason_code IS NOT NULL)) OR (decision = ANY (ARRAY['APPROVE'::text, 'PUBLISH'::text]))))
);


--
-- Name: sessions; Type: TABLE; Schema: console; Owner: -
--

CREATE TABLE console.sessions (
    session_id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    token_hash text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone
);


--
-- Name: sync_events; Type: TABLE; Schema: console; Owner: -
--

CREATE TABLE console.sync_events (
    sync_id uuid DEFAULT gen_random_uuid() NOT NULL,
    synced_at timestamp with time zone DEFAULT now() NOT NULL,
    synced_by text,
    source_mode text NOT NULL,
    target_mode text NOT NULL,
    comment text,
    status text DEFAULT 'logged'::text NOT NULL,
    tables_changed jsonb DEFAULT '[]'::jsonb NOT NULL,
    row_counts jsonb DEFAULT '{}'::jsonb NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: users; Type: TABLE; Schema: console; Owner: -
--

CREATE TABLE console.users (
    user_id uuid DEFAULT gen_random_uuid() NOT NULL,
    email public.citext NOT NULL,
    display_name text NOT NULL,
    role text DEFAULT 'admin'::text NOT NULL,
    password_hash text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    last_login_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    email_verified boolean DEFAULT false NOT NULL,
    email_verified_at timestamp with time zone,
    CONSTRAINT chk_console_users_role CHECK ((role = ANY (ARRAY['admin'::text, 'editor'::text, 'viewer'::text])))
);


--
-- Name: v_audit_daily; Type: VIEW; Schema: console; Owner: -
--

CREATE VIEW console.v_audit_daily AS
 SELECT date_trunc('day'::text, created_at) AS day,
    phase,
    ok,
    count(*) AS n
   FROM console.audit_events
  GROUP BY (date_trunc('day'::text, created_at)), phase, ok
  ORDER BY (date_trunc('day'::text, created_at)) DESC, phase, ok;


--
-- Name: v_decisions_daily; Type: VIEW; Schema: console; Owner: -
--

CREATE VIEW console.v_decisions_daily AS
 SELECT date_trunc('day'::text, created_at) AS day,
    phase,
    decision,
    count(*) AS n
   FROM console.phase_decisions
  GROUP BY (date_trunc('day'::text, created_at)), phase, decision
  ORDER BY (date_trunc('day'::text, created_at)) DESC, phase, decision;


--
-- Name: workspace_state; Type: TABLE; Schema: console; Owner: -
--

CREATE TABLE console.workspace_state (
    user_id uuid NOT NULL,
    current_phase smallint,
    current_subtab text,
    current_route_id text,
    current_stop_id text,
    last_candidate_id text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: node_place_map; Type: TABLE; Schema: geo_prod; Owner: -
--

CREATE TABLE geo_prod.node_place_map (
    node_id uuid NOT NULL,
    place_id uuid NOT NULL,
    confidence double precision DEFAULT 0.0 NOT NULL,
    mapping_source text DEFAULT 'phase2_auto'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT node_place_map_mapping_source_check CHECK ((mapping_source = ANY (ARRAY['phase2_auto'::text, 'user_selected'::text, 'manual_override'::text])))
);


--
-- Name: place_alias_embeddings; Type: TABLE; Schema: geo_prod; Owner: -
--

CREATE TABLE geo_prod.place_alias_embeddings (
    alias_id uuid NOT NULL,
    place_id uuid NOT NULL,
    model_name text NOT NULL,
    model_version text,
    dim integer NOT NULL,
    embedding public.vector(384) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: place_aliases; Type: TABLE; Schema: geo_prod; Owner: -
--

CREATE TABLE geo_prod.place_aliases (
    alias_id uuid DEFAULT gen_random_uuid() NOT NULL,
    place_id uuid NOT NULL,
    alias text NOT NULL,
    normalized_alias text NOT NULL,
    alias_kind text DEFAULT 'alt'::text NOT NULL,
    lang text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT place_aliases_alias_kind_check CHECK ((alias_kind = ANY (ARRAY['official'::text, 'short'::text, 'alt'::text, 'abbr'::text, 'historic'::text, 'typo_common'::text])))
);


--
-- Name: place_embeddings; Type: TABLE; Schema: geo_prod; Owner: -
--

CREATE TABLE geo_prod.place_embeddings (
    place_id uuid NOT NULL,
    model_name text NOT NULL,
    model_version text,
    dim integer NOT NULL,
    embedding public.vector(384) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: place_history; Type: TABLE; Schema: geo_prod; Owner: -
--

CREATE TABLE geo_prod.place_history (
    history_id uuid DEFAULT gen_random_uuid() NOT NULL,
    place_id uuid NOT NULL,
    previous_canonical_name text,
    previous_place_type text,
    previous_status text,
    previous_geom public.geometry(Point,4326),
    changed_by text NOT NULL,
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    change_reason text
);


--
-- Name: places; Type: TABLE; Schema: geo_prod; Owner: -
--

CREATE TABLE geo_prod.places (
    place_id uuid DEFAULT gen_random_uuid() NOT NULL,
    canonical_name text NOT NULL,
    place_type text NOT NULL,
    region text,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    geom public.geometry(Point,4326),
    province text DEFAULT 'sample_region'::text NOT NULL,
    canonical_name_method text,
    needs_operator_review boolean DEFAULT false,
    proposed_canonical_name text,
    canonical_name_renamed_at timestamp with time zone,
    CONSTRAINT places_place_type_check CHECK ((place_type = ANY (ARRAY['STOP'::text, 'POI'::text, 'STATION'::text, 'TERMINAL'::text, 'OTHER'::text]))),
    CONSTRAINT places_status_check CHECK ((status = ANY (ARRAY['active'::text, 'deprecated'::text])))
);


--
-- Name: v_active_alias_embeddings; Type: VIEW; Schema: geo_prod; Owner: -
--

CREATE VIEW geo_prod.v_active_alias_embeddings AS
 SELECT e.alias_id,
    e.place_id,
    e.model_name,
    e.dim,
    e.embedding,
    e.created_at,
    e.updated_at
   FROM ((geo_prod.place_alias_embeddings e
     JOIN geo_prod.place_aliases a ON ((a.alias_id = e.alias_id)))
     JOIN geo_prod.places p ON ((p.place_id = a.place_id)))
  WHERE (p.status = 'active'::text);


--
-- Name: v_active_aliases; Type: VIEW; Schema: geo_prod; Owner: -
--

CREATE VIEW geo_prod.v_active_aliases AS
 SELECT a.alias_id,
    a.place_id,
    a.alias,
    a.normalized_alias,
    a.alias_kind,
    a.lang,
    a.created_at,
    a.updated_at
   FROM (geo_prod.place_aliases a
     JOIN geo_prod.places p ON ((p.place_id = a.place_id)))
  WHERE (p.status = 'active'::text);


--
-- Name: v_active_places; Type: VIEW; Schema: geo_prod; Owner: -
--

CREATE VIEW geo_prod.v_active_places AS
 SELECT place_id,
    canonical_name,
    place_type,
    region,
    status,
    geom,
    created_at,
    updated_at
   FROM geo_prod.places p
  WHERE (status = 'active'::text);


--
-- Name: nodes; Type: TABLE; Schema: node_prod; Owner: -
--

CREATE TABLE node_prod.nodes (
    node_id uuid NOT NULL,
    geom public.geometry(Point,4326) NOT NULL,
    node_type text NOT NULL,
    name text,
    ref text,
    operator text,
    tag_kind text DEFAULT 'bus_stop'::text NOT NULL,
    source text DEFAULT 'work_review'::text NOT NULL,
    source_node_set_id uuid,
    chosen_candidate_id uuid,
    chosen_tags jsonb DEFAULT '{}'::jsonb NOT NULL,
    confidence double precision DEFAULT 0.0 NOT NULL,
    approved_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    province text DEFAULT 'sample_region'::text NOT NULL,
    source_type text,
    osm_id bigint,
    synthetic_created_at timestamp with time zone,
    synthetic_created_by text,
    synthetic_confidence text,
    synthetic_review_state text,
    superseded_by uuid,
    poi_anchor_osm_id bigint,
    poi_anchor_class text,
    poi_anchor_access_point text,
    poi_to_path_distance_m real,
    path_projection_distance_m real,
    research_to_projection_distance_m real,
    semantic_spatial_conflict boolean DEFAULT false NOT NULL,
    osm_route_fill_context text,
    CONSTRAINT nodes_node_type_check CHECK ((node_type = ANY (ARRAY['STOP'::text, 'POI'::text]))),
    CONSTRAINT nodes_poi_anchor_access_point_check CHECK (((poi_anchor_access_point IS NULL) OR (poi_anchor_access_point = ANY (ARRAY['entrance_node'::text, 'road_projection'::text, 'centroid_fallback'::text])))),
    CONSTRAINT nodes_source_type_check CHECK (((source_type IS NULL) OR (source_type = ANY (ARRAY['osm'::text, 'backfill'::text, 'poi_anchored_path_projected'::text, 'path_corridor_projected'::text, 'path_intersection'::text, 'research_coords_path_snapped'::text, 'pure_synthesis'::text, 'gps_trace'::text])))),
    CONSTRAINT nodes_synthetic_confidence_check CHECK (((synthetic_confidence IS NULL) OR (synthetic_confidence = ANY (ARRAY['low'::text, 'medium'::text, 'high'::text])))),
    CONSTRAINT nodes_synthetic_review_state_check CHECK (((synthetic_review_state IS NULL) OR (synthetic_review_state = ANY (ARRAY['pending'::text, 'verified'::text, 'rejected'::text]))))
);


--
-- Name: COLUMN nodes.source_type; Type: COMMENT; Schema: node_prod; Owner: -
--

COMMENT ON COLUMN node_prod.nodes.source_type IS '8-value enum capturing the provenance class: osm | backfill | <6 synthesis values>. Distinct from the free-text `source` column, which carries a human-readable audit string (e.g. "poi_anchored_path_projected:route_CAL-03:anchor_Y_de_Calsig"). Legacy rows have source_type = NULL.';


--
-- Name: COLUMN nodes.osm_id; Type: COMMENT; Schema: node_prod; Owner: -
--

COMMENT ON COLUMN node_prod.nodes.osm_id IS 'OSM element id. Positive for real OSM elements; negative (from synthetic_osm_id_seq) for synthesis-stage rows. Legacy rows have osm_id = NULL — the indirect link via chosen_candidate_id remains.';


--
-- Name: COLUMN nodes.synthetic_created_at; Type: COMMENT; Schema: node_prod; Owner: -
--

COMMENT ON COLUMN node_prod.nodes.synthetic_created_at IS 'Timestamp the synthetic node was written by hades-path-aware-synthesis. NULL for osm/backfill rows.';


--
-- Name: COLUMN nodes.synthetic_created_by; Type: COMMENT; Schema: node_prod; Owner: -
--

COMMENT ON COLUMN node_prod.nodes.synthetic_created_by IS 'Skill or operator name that produced the synthetic node (e.g. "hades-path-aware-synthesis:3a_poi_on_path").';


--
-- Name: COLUMN nodes.synthetic_confidence; Type: COMMENT; Schema: node_prod; Owner: -
--

COMMENT ON COLUMN node_prod.nodes.synthetic_confidence IS 'Initial synthesis confidence per hades-path-aware-synthesis §7 tier mapping. Promotion updates this.';


--
-- Name: COLUMN nodes.synthetic_review_state; Type: COMMENT; Schema: node_prod; Owner: -
--

COMMENT ON COLUMN node_prod.nodes.synthetic_review_state IS 'pending = awaits operator review, verified = operator approved, rejected = operator rejected (row retained; FK nulled).';


--
-- Name: COLUMN nodes.superseded_by; Type: COMMENT; Schema: node_prod; Owner: -
--

COMMENT ON COLUMN node_prod.nodes.superseded_by IS 'Set when a canonical OSM node (or higher-confidence synthetic) replaces this row in the route stop sequence. Row is retained for audit; no DELETE.';


--
-- Name: COLUMN nodes.poi_anchor_osm_id; Type: COMMENT; Schema: node_prod; Owner: -
--

COMMENT ON COLUMN node_prod.nodes.poi_anchor_osm_id IS 'OSM element id of the POI used as anchor (positive for real OSM element; NULL for stages that did not use a POI anchor).';


--
-- Name: COLUMN nodes.poi_anchor_class; Type: COMMENT; Schema: node_prod; Owner: -
--

COMMENT ON COLUMN node_prod.nodes.poi_anchor_class IS 'OSM class of the POI anchor (e.g. amenity=marketplace, shop=supermarket, amenity=place_of_worship).';


--
-- Name: COLUMN nodes.poi_anchor_access_point; Type: COMMENT; Schema: node_prod; Owner: -
--

COMMENT ON COLUMN node_prod.nodes.poi_anchor_access_point IS 'entrance_node = used explicit entrance=yes; road_projection = nearest-road projection of POI centroid; centroid_fallback = POI centroid used directly (only for point POIs).';


--
-- Name: COLUMN nodes.poi_to_path_distance_m; Type: COMMENT; Schema: node_prod; Owner: -
--

COMMENT ON COLUMN node_prod.nodes.poi_to_path_distance_m IS 'Haversine distance (metres) from POI anchor centroid to the route inferred_path_polyline. Hard ceiling 80m per §5 stage 3a.';


--
-- Name: COLUMN nodes.path_projection_distance_m; Type: COMMENT; Schema: node_prod; Owner: -
--

COMMENT ON COLUMN node_prod.nodes.path_projection_distance_m IS 'Distance (metres) between the raw POI/research coord and its final projection onto the inferred path.';


--
-- Name: COLUMN nodes.research_to_projection_distance_m; Type: COMMENT; Schema: node_prod; Owner: -
--

COMMENT ON COLUMN node_prod.nodes.research_to_projection_distance_m IS 'Distance (metres) between Deep Research provided coords and the final snapped position. Stage 3d: <=30m full-confidence, 30-80m low-confidence, >80m fail.';


--
-- Name: COLUMN nodes.semantic_spatial_conflict; Type: COMMENT; Schema: node_prod; Owner: -
--

COMMENT ON COLUMN node_prod.nodes.semantic_spatial_conflict IS 'TRUE when POI inference and path inference produced divergent positions (> 80m apart, or contradictory road). Routed to synthetic_review/semantic_spatial_conflicts/.';


--
-- Name: COLUMN nodes.osm_route_fill_context; Type: COMMENT; Schema: node_prod; Owner: -
--

COMMENT ON COLUMN node_prod.nodes.osm_route_fill_context IS 'For osm_route_fill-origin synthetics: "relation_<osm_id>_gap_<idx>". NULL for other synthesis stages.';


--
-- Name: v_place_points; Type: VIEW; Schema: geo_prod; Owner: -
--

CREATE VIEW geo_prod.v_place_points AS
 SELECT p.place_id,
    p.canonical_name,
    p.place_type,
    m.node_id,
    COALESCE(m.confidence, (1.0)::double precision) AS confidence,
    COALESCE(m.mapping_source, 'place_geom'::text) AS mapping_source,
    n.node_type,
    COALESCE(n.geom, p.geom) AS geom,
    public.st_y(COALESCE(n.geom, p.geom)) AS lat,
    public.st_x(COALESCE(n.geom, p.geom)) AS lon
   FROM ((geo_prod.places p
     LEFT JOIN geo_prod.node_place_map m ON ((m.place_id = p.place_id)))
     LEFT JOIN node_prod.nodes n ON ((n.node_id = m.node_id)))
  WHERE (COALESCE(n.geom, p.geom) IS NOT NULL);


--
-- Name: v_place_summary; Type: VIEW; Schema: geo_prod; Owner: -
--

CREATE VIEW geo_prod.v_place_summary AS
 SELECT p.place_id,
    p.canonical_name,
    p.place_type,
    (count(m.node_id))::integer AS n_nodes,
    avg(m.confidence) AS avg_confidence,
    min(m.confidence) AS min_confidence,
    max(m.confidence) AS max_confidence,
    COALESCE(
        CASE
            WHEN (count(n.geom) > 0) THEN (public.st_centroid(public.st_collect(n.geom)))::public.geometry(Point,4326)
            ELSE NULL::public.geometry(Point,4326)
        END, p.geom) AS center_geom,
        CASE
            WHEN (count(n.geom) > 0) THEN (public.st_extent(n.geom))::text
            WHEN (p.geom IS NOT NULL) THEN public.st_astext(public.st_envelope(p.geom))
            ELSE NULL::text
        END AS bbox
   FROM ((geo_prod.places p
     LEFT JOIN geo_prod.node_place_map m ON ((m.place_id = p.place_id)))
     LEFT JOIN node_prod.nodes n ON ((n.node_id = m.node_id)))
  GROUP BY p.place_id, p.canonical_name, p.place_type, p.geom;


--
-- Name: extract_runs; Type: TABLE; Schema: geo_raw; Owner: -
--

CREATE TABLE geo_raw.extract_runs (
    extract_run_id uuid DEFAULT gen_random_uuid() NOT NULL,
    source_node_set_id uuid,
    context_key text,
    status text DEFAULT 'ok'::text NOT NULL,
    runtime_ms integer,
    n_nodes_seen integer,
    n_evidence integer,
    extracted_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT chk_geo_extract_runs_status CHECK ((status = ANY (ARRAY['ok'::text, 'error'::text])))
);


--
-- Name: name_evidence; Type: TABLE; Schema: geo_raw; Owner: -
--

CREATE TABLE geo_raw.name_evidence (
    extract_run_id uuid NOT NULL,
    node_id uuid NOT NULL,
    source text NOT NULL,
    raw_text text NOT NULL,
    lang text,
    weight_hint double precision DEFAULT 1.0 NOT NULL,
    tags_snapshot jsonb DEFAULT '{}'::jsonb NOT NULL,
    inserted_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT chk_geo_name_evidence_source CHECK ((source <> ''::text))
);


--
-- Name: alias_candidates; Type: TABLE; Schema: geo_work; Owner: -
--

CREATE TABLE geo_work.alias_candidates (
    alias_candidate_id uuid DEFAULT gen_random_uuid() NOT NULL,
    place_candidate_id uuid NOT NULL,
    alias text NOT NULL,
    alias_kind text DEFAULT 'alt'::text NOT NULL,
    lang text,
    score double precision,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT alias_candidates_alias_kind_check CHECK ((alias_kind = ANY (ARRAY['official'::text, 'short'::text, 'alt'::text, 'abbr'::text, 'historic'::text, 'typo_common'::text])))
);


--
-- Name: model_registry; Type: TABLE; Schema: geo_work; Owner: -
--

CREATE TABLE geo_work.model_registry (
    model_name text NOT NULL,
    version text NOT NULL,
    artifact jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: node_geo_context; Type: TABLE; Schema: geo_work; Owner: -
--

CREATE TABLE geo_work.node_geo_context (
    extract_run_id uuid NOT NULL,
    context_key text,
    node_id uuid NOT NULL,
    lat double precision,
    lon double precision,
    geohash7 text,
    transit_density_300m integer DEFAULT 0 NOT NULL,
    poi_density_300m integer DEFAULT 0 NOT NULL,
    tag_stop_weight double precision DEFAULT 0 NOT NULL,
    tag_poi_weight double precision DEFAULT 0 NOT NULL,
    features jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: node_place_map_work; Type: TABLE; Schema: geo_work; Owner: -
--

CREATE TABLE geo_work.node_place_map_work (
    place_set_id uuid NOT NULL,
    node_id uuid NOT NULL,
    place_candidate_id uuid NOT NULL,
    confidence double precision DEFAULT 0.0 NOT NULL,
    mapping_source text DEFAULT 'auto'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT node_place_map_work_mapping_source_check CHECK ((mapping_source = ANY (ARRAY['auto'::text, 'manual'::text])))
);


--
-- Name: place_candidate_sets; Type: TABLE; Schema: geo_work; Owner: -
--

CREATE TABLE geo_work.place_candidate_sets (
    place_set_id uuid DEFAULT gen_random_uuid() NOT NULL,
    source_extract_run_id uuid NOT NULL,
    context_key text,
    params_used jsonb DEFAULT '{}'::jsonb NOT NULL,
    rank_score double precision,
    rank_model_ver text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: place_candidates; Type: TABLE; Schema: geo_work; Owner: -
--

CREATE TABLE geo_work.place_candidates (
    place_candidate_id uuid DEFAULT gen_random_uuid() NOT NULL,
    place_set_id uuid NOT NULL,
    proposed_canonical_name text NOT NULL,
    proposed_place_type text NOT NULL,
    center_geom public.geometry(Point,4326),
    provenance jsonb DEFAULT '{}'::jsonb NOT NULL,
    score double precision,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    model_place_type text,
    model_place_type_score double precision,
    CONSTRAINT place_candidates_model_place_type_check CHECK ((model_place_type = ANY (ARRAY['STOP'::text, 'POI'::text, 'STATION'::text, 'TERMINAL'::text, 'OTHER'::text]))),
    CONSTRAINT place_candidates_proposed_place_type_check CHECK ((proposed_place_type = ANY (ARRAY['STOP'::text, 'POI'::text, 'STATION'::text, 'TERMINAL'::text, 'OTHER'::text])))
);


--
-- Name: place_name_candidates; Type: TABLE; Schema: geo_work; Owner: -
--

CREATE TABLE geo_work.place_name_candidates (
    name_candidate_id uuid DEFAULT gen_random_uuid() NOT NULL,
    place_set_id uuid NOT NULL,
    place_candidate_id uuid NOT NULL,
    candidate_name text NOT NULL,
    candidate_name_norm text NOT NULL,
    source_kind text DEFAULT 'generated'::text NOT NULL,
    score double precision,
    model_score double precision,
    model_rank integer,
    selected_by_model boolean DEFAULT false NOT NULL,
    features jsonb DEFAULT '{}'::jsonb NOT NULL,
    provenance jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT place_name_candidates_source_kind_check CHECK ((source_kind = ANY (ARRAY['generated'::text, 'alias'::text, 'tag'::text, 'model'::text, 'manual'::text, 'custom'::text])))
);


--
-- Name: place_name_feedback; Type: TABLE; Schema: geo_work; Owner: -
--

CREATE TABLE geo_work.place_name_feedback (
    feedback_id uuid DEFAULT gen_random_uuid() NOT NULL,
    place_set_id uuid NOT NULL,
    place_candidate_id uuid NOT NULL,
    chosen_name_candidate_id uuid,
    chosen_name text NOT NULL,
    chosen_name_norm text NOT NULL,
    chosen_source text DEFAULT 'user_pick'::text NOT NULL,
    rejected_name_candidate_ids uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    reviewer text,
    context jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT place_name_feedback_chosen_source_check CHECK ((chosen_source = ANY (ARRAY['model_pick'::text, 'user_pick'::text, 'custom'::text, 'phase3_implicit'::text])))
);


--
-- Name: place_set_metrics; Type: TABLE; Schema: geo_work; Owner: -
--

CREATE TABLE geo_work.place_set_metrics (
    place_set_id uuid NOT NULL,
    n_places integer DEFAULT 0 NOT NULL,
    n_aliases integer DEFAULT 0 NOT NULL,
    n_nodes_mapped integer DEFAULT 0 NOT NULL,
    alias_conflict_rate double precision DEFAULT 0.0 NOT NULL,
    avg_confidence double precision,
    computed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: place_set_overview; Type: VIEW; Schema: geo_work; Owner: -
--

CREATE VIEW geo_work.place_set_overview AS
 WITH ranked_candidates AS (
         SELECT pc.place_set_id,
            pc.place_candidate_id,
            pc.proposed_canonical_name,
            pc.score,
            row_number() OVER (PARTITION BY pc.place_set_id ORDER BY pc.score DESC NULLS LAST, pc.proposed_canonical_name) AS rn
           FROM geo_work.place_candidates pc
        )
 SELECT pcs.place_set_id,
    pcs.created_at,
    pcs.context_key,
    pcs.source_extract_run_id,
    rc.proposed_canonical_name AS display_name,
    rc.score AS display_score,
    count(pc2.place_candidate_id) AS n_candidates
   FROM ((geo_work.place_candidate_sets pcs
     JOIN ranked_candidates rc ON (((rc.place_set_id = pcs.place_set_id) AND (rc.rn = 1))))
     JOIN geo_work.place_candidates pc2 ON ((pc2.place_set_id = pcs.place_set_id)))
  GROUP BY pcs.place_set_id, pcs.created_at, pcs.context_key, pcs.source_extract_run_id, rc.proposed_canonical_name, rc.score;


--
-- Name: poi_stop_feedback; Type: TABLE; Schema: geo_work; Owner: -
--

CREATE TABLE geo_work.poi_stop_feedback (
    feedback_id uuid DEFAULT gen_random_uuid() NOT NULL,
    place_set_id uuid NOT NULL,
    place_candidate_id uuid NOT NULL,
    chosen_place_type text NOT NULL,
    chosen_source text DEFAULT 'user_pick'::text NOT NULL,
    reviewer text,
    context jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT poi_stop_feedback_chosen_place_type_check CHECK ((chosen_place_type = ANY (ARRAY['STOP'::text, 'POI'::text, 'STATION'::text, 'TERMINAL'::text, 'OTHER'::text]))),
    CONSTRAINT poi_stop_feedback_chosen_source_check CHECK ((chosen_source = ANY (ARRAY['model_pick'::text, 'user_pick'::text, 'custom'::text, 'phase3_implicit'::text])))
);


--
-- Name: selection_log; Type: TABLE; Schema: geo_work; Owner: -
--

CREATE TABLE geo_work.selection_log (
    selection_id uuid DEFAULT gen_random_uuid() NOT NULL,
    phase integer NOT NULL,
    object_type text NOT NULL,
    chosen_set_id uuid NOT NULL,
    rejected_set_ids uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    metrics jsonb DEFAULT '{}'::jsonb NOT NULL,
    context jsonb DEFAULT '{}'::jsonb NOT NULL,
    selected_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT selection_log_object_type_check CHECK ((object_type = 'places'::text)),
    CONSTRAINT selection_log_phase_check CHECK ((phase = 2))
);


--
-- Name: v_node_semantic_evidence; Type: VIEW; Schema: geo_work; Owner: -
--

CREATE VIEW geo_work.v_node_semantic_evidence AS
 SELECT node_id,
    node_type,
    geom,
    chosen_tags,
    NULLIF((chosen_tags ->> 'name'::text), ''::text) AS name,
    NULLIF((chosen_tags ->> 'name:es'::text), ''::text) AS name_es,
    NULLIF((chosen_tags ->> 'official_name'::text), ''::text) AS official_name,
    NULLIF((chosen_tags ->> 'short_name'::text), ''::text) AS short_name,
    NULLIF((chosen_tags ->> 'alt_name'::text), ''::text) AS alt_name,
    NULLIF((chosen_tags ->> 'ref'::text), ''::text) AS ref,
    NULLIF((chosen_tags ->> 'operator'::text), ''::text) AS operator,
    NULLIF((chosen_tags ->> 'network'::text), ''::text) AS network
   FROM node_prod.nodes n;


--
-- Name: v_place_set_points; Type: VIEW; Schema: geo_work; Owner: -
--

CREATE VIEW geo_work.v_place_set_points AS
 SELECT m.place_set_id,
    m.place_candidate_id,
    pc.proposed_canonical_name,
    pc.proposed_place_type,
    m.node_id,
    m.confidence,
    m.mapping_source,
    n.node_type,
    n.geom,
    public.st_y(n.geom) AS lat,
    public.st_x(n.geom) AS lon
   FROM ((geo_work.node_place_map_work m
     JOIN node_prod.nodes n ON ((n.node_id = m.node_id)))
     JOIN geo_work.place_candidates pc ON ((pc.place_candidate_id = m.place_candidate_id)));


--
-- Name: gtfs_artifacts; Type: TABLE; Schema: gtfs; Owner: -
--

CREATE TABLE gtfs.gtfs_artifacts (
    artifact_id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    status text DEFAULT 'pending_approval'::text NOT NULL,
    zip_path text NOT NULL,
    file_hash text NOT NULL,
    summary_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    approval_token text NOT NULL,
    approved_at timestamp with time zone,
    approved_by text,
    rejected_at timestamp with time zone,
    rejected_by text,
    downloaded_at timestamp with time zone,
    notes text,
    CONSTRAINT gtfs_artifacts_status_check CHECK ((status = ANY (ARRAY['pending_approval'::text, 'approved'::text, 'rejected'::text, 'expired'::text, 'downloaded'::text])))
);


--
-- Name: gtfs_audit_log; Type: TABLE; Schema: gtfs; Owner: -
--

CREATE TABLE gtfs.gtfs_audit_log (
    audit_id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    artifact_id uuid,
    action text NOT NULL,
    actor text NOT NULL,
    payload_json jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: gtfs_notifications; Type: TABLE; Schema: gtfs; Owner: -
--

CREATE TABLE gtfs.gtfs_notifications (
    notification_id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    artifact_id uuid NOT NULL,
    channel text NOT NULL,
    provider text NOT NULL,
    to_address text NOT NULL,
    status text DEFAULT 'queued'::text NOT NULL,
    provider_message_id text,
    error_text text,
    CONSTRAINT gtfs_notifications_channel_check CHECK ((channel = 'whatsapp'::text)),
    CONSTRAINT gtfs_notifications_provider_check CHECK ((provider = 'meta_cloud_api'::text)),
    CONSTRAINT gtfs_notifications_status_check CHECK ((status = ANY (ARRAY['queued'::text, 'sent'::text, 'failed'::text])))
);


--
-- Name: feed_versions; Type: TABLE; Schema: gtfs_prod; Owner: -
--

CREATE TABLE gtfs_prod.feed_versions (
    feed_version_id uuid DEFAULT gen_random_uuid() NOT NULL,
    export_run_id uuid NOT NULL,
    gtfs_zip_path text NOT NULL,
    validator_report jsonb DEFAULT '{}'::jsonb NOT NULL,
    published_by text,
    published_at timestamp with time zone DEFAULT now() NOT NULL,
    is_current boolean DEFAULT true NOT NULL,
    gtfs_id text
);


--
-- Name: agency_catalog; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.agency_catalog (
    agency_id text NOT NULL,
    agency_name text NOT NULL,
    agency_name_norm text NOT NULL,
    agency_url text DEFAULT 'https://example.com'::text NOT NULL,
    agency_timezone text DEFAULT 'America/Guayaquil'::text NOT NULL,
    agency_lang text DEFAULT 'es'::text,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agency_match_requests; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.agency_match_requests (
    request_id uuid DEFAULT gen_random_uuid() NOT NULL,
    route_id uuid NOT NULL,
    source_operator_name text,
    source_operator_norm text,
    suggested_agency_id text,
    suggested_agency_name text,
    score double precision,
    status text DEFAULT 'pending'::text NOT NULL,
    resolved_agency_id text,
    resolution_note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_at timestamp with time zone,
    source_agency_name text,
    source_agency_norm text
);


--
-- Name: calendar_exceptions; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.calendar_exceptions (
    exception_id uuid DEFAULT gen_random_uuid() NOT NULL,
    profile_id uuid NOT NULL,
    service_date date NOT NULL,
    exception_type smallint NOT NULL,
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT calendar_exceptions_exception_type_check CHECK ((exception_type = ANY (ARRAY[1, 2])))
);


--
-- Name: export_runs; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.export_runs (
    export_run_id uuid DEFAULT gen_random_uuid() NOT NULL,
    status text DEFAULT 'draft'::text NOT NULL,
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    summary jsonb DEFAULT '{}'::jsonb NOT NULL,
    validator_report jsonb DEFAULT '{}'::jsonb NOT NULL,
    output_zip_path text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    gtfs_id text NOT NULL
);


--
-- Name: gtfs_agency; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.gtfs_agency (
    export_run_id uuid NOT NULL,
    agency_id text NOT NULL,
    agency_name text NOT NULL,
    agency_url text NOT NULL,
    agency_timezone text NOT NULL,
    agency_lang text,
    gtfs_id text,
    province text DEFAULT 'sample_region'::text NOT NULL
);


--
-- Name: gtfs_builds; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.gtfs_builds (
    gtfs_id text NOT NULL,
    export_run_id uuid,
    build_name text,
    status text DEFAULT 'draft'::text NOT NULL,
    notes text,
    created_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: gtfs_calendar; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.gtfs_calendar (
    export_run_id uuid NOT NULL,
    service_id text NOT NULL,
    monday smallint NOT NULL,
    tuesday smallint NOT NULL,
    wednesday smallint NOT NULL,
    thursday smallint NOT NULL,
    friday smallint NOT NULL,
    saturday smallint NOT NULL,
    sunday smallint NOT NULL,
    start_date text NOT NULL,
    end_date text NOT NULL,
    gtfs_id text
);


--
-- Name: gtfs_calendar_dates; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.gtfs_calendar_dates (
    export_run_id uuid NOT NULL,
    service_id text NOT NULL,
    date text NOT NULL,
    exception_type smallint NOT NULL,
    gtfs_id text
);


--
-- Name: gtfs_frequencies; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.gtfs_frequencies (
    export_run_id uuid NOT NULL,
    trip_id text NOT NULL,
    start_time text NOT NULL,
    end_time text NOT NULL,
    headway_secs integer NOT NULL,
    exact_times smallint DEFAULT 0 NOT NULL,
    gtfs_id text
);


--
-- Name: gtfs_loadings; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.gtfs_loadings (
    loading_id uuid DEFAULT gen_random_uuid() NOT NULL,
    gtfs_id text NOT NULL,
    export_run_id uuid NOT NULL,
    source text DEFAULT 'uploaded_gtfs'::text NOT NULL,
    file_name text,
    file_size_bytes bigint,
    status text DEFAULT 'loaded'::text NOT NULL,
    summary jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: gtfs_overrides; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.gtfs_overrides (
    override_id uuid DEFAULT gen_random_uuid() NOT NULL,
    export_run_id uuid NOT NULL,
    table_name text NOT NULL,
    row_key jsonb NOT NULL,
    patched_row jsonb NOT NULL,
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    gtfs_id text
);


--
-- Name: gtfs_routes; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.gtfs_routes (
    export_run_id uuid NOT NULL,
    route_id text NOT NULL,
    agency_id text,
    route_short_name text,
    route_long_name text NOT NULL,
    route_type integer DEFAULT 3 NOT NULL,
    route_color text,
    route_text_color text,
    gtfs_id text
);


--
-- Name: gtfs_shapes; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.gtfs_shapes (
    export_run_id uuid NOT NULL,
    shape_id text NOT NULL,
    shape_pt_lat double precision NOT NULL,
    shape_pt_lon double precision NOT NULL,
    shape_pt_sequence integer NOT NULL,
    shape_dist_traveled double precision,
    gtfs_id text
);


--
-- Name: gtfs_stop_times; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.gtfs_stop_times (
    export_run_id uuid NOT NULL,
    trip_id text NOT NULL,
    arrival_time text NOT NULL,
    departure_time text NOT NULL,
    stop_id text NOT NULL,
    stop_sequence integer NOT NULL,
    timepoint smallint DEFAULT 1,
    gtfs_id text,
    shape_dist_traveled numeric
);


--
-- Name: gtfs_stops; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.gtfs_stops (
    export_run_id uuid NOT NULL,
    stop_id text NOT NULL,
    stop_name text NOT NULL,
    stop_lat double precision NOT NULL,
    stop_lon double precision NOT NULL,
    location_type smallint DEFAULT 0 NOT NULL,
    parent_station text,
    gtfs_id text
);


--
-- Name: gtfs_trips; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.gtfs_trips (
    export_run_id uuid NOT NULL,
    route_id text NOT NULL,
    service_id text NOT NULL,
    trip_id text NOT NULL,
    trip_headsign text,
    direction_id smallint,
    shape_id text,
    block_id text,
    gtfs_id text
);


--
-- Name: revision_events; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.revision_events (
    event_id uuid DEFAULT gen_random_uuid() NOT NULL,
    export_run_id uuid NOT NULL,
    event_type text NOT NULL,
    table_name text NOT NULL,
    row_key jsonb DEFAULT '{}'::jsonb NOT NULL,
    before_row jsonb DEFAULT '{}'::jsonb NOT NULL,
    after_row jsonb DEFAULT '{}'::jsonb NOT NULL,
    edited_by text,
    edited_at timestamp with time zone DEFAULT now() NOT NULL,
    gtfs_id text
);


--
-- Name: route_agency_links; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.route_agency_links (
    route_id uuid NOT NULL,
    agency_id text NOT NULL,
    match_score double precision,
    match_method text,
    source_operator_name text,
    status text DEFAULT 'linked'::text NOT NULL,
    updated_by text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    source_agency_name text
);


--
-- Name: route_runtime_estimate_bindings; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.route_runtime_estimate_bindings (
    route_id uuid NOT NULL,
    direction_id smallint NOT NULL,
    estimate_id uuid NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT route_runtime_estimate_bindings_direction_id_check CHECK ((direction_id = ANY (ARRAY[0, 1])))
);


--
-- Name: route_schedule_profiles; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.route_schedule_profiles (
    profile_id uuid DEFAULT gen_random_uuid() NOT NULL,
    route_id uuid NOT NULL,
    direction_id smallint DEFAULT 0 NOT NULL,
    service_name text NOT NULL,
    runtime_secs integer DEFAULT 3600 NOT NULL,
    dwell_secs integer DEFAULT 20 NOT NULL,
    shape_source text DEFAULT 'route_prod_geom'::text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    n_blocks integer DEFAULT 1 NOT NULL,
    CONSTRAINT chk_route_schedule_profiles_n_blocks CHECK ((n_blocks >= 1)),
    CONSTRAINT route_schedule_profiles_direction_id_check CHECK ((direction_id = ANY (ARRAY[0, 1])))
);


--
-- Name: runtime_catalog_items; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.runtime_catalog_items (
    catalog_key text NOT NULL,
    item_code text NOT NULL,
    item_name text NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    source text DEFAULT 'manual'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: runtime_congestion_hotspots; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.runtime_congestion_hotspots (
    id integer NOT NULL,
    route_id uuid,
    location text,
    lat numeric,
    lon numeric,
    peak_am_delay_min numeric,
    off_peak_delay_min numeric,
    peak_pm_delay_min numeric,
    cause text,
    source text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: runtime_congestion_hotspots_id_seq; Type: SEQUENCE; Schema: gtfs_work; Owner: -
--

CREATE SEQUENCE gtfs_work.runtime_congestion_hotspots_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: runtime_congestion_hotspots_id_seq; Type: SEQUENCE OWNED BY; Schema: gtfs_work; Owner: -
--

ALTER SEQUENCE gtfs_work.runtime_congestion_hotspots_id_seq OWNED BY gtfs_work.runtime_congestion_hotspots.id;


--
-- Name: runtime_route_bindings; Type: VIEW; Schema: gtfs_work; Owner: -
--

CREATE VIEW gtfs_work.runtime_route_bindings AS
 SELECT route_id,
    direction_id,
    estimate_id,
    updated_at AS bound_at,
    'active'::text AS status
   FROM gtfs_work.route_runtime_estimate_bindings;


--
-- Name: runtime_route_estimates; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.runtime_route_estimates (
    estimate_id uuid DEFAULT gen_random_uuid() NOT NULL,
    route_id uuid NOT NULL,
    direction_id smallint NOT NULL,
    area_profile_code text,
    speed_profile_code text,
    dwell_profile_code text,
    intersection_profile_code text,
    peak_profile_code text,
    confidence_profile_code text,
    metrics jsonb DEFAULT '{}'::jsonb NOT NULL,
    model_inputs jsonb DEFAULT '{}'::jsonb NOT NULL,
    fallback_reason jsonb DEFAULT '[]'::jsonb NOT NULL,
    route_prior_match jsonb DEFAULT '{}'::jsonb NOT NULL,
    estimated_at timestamp with time zone DEFAULT now() NOT NULL,
    status text DEFAULT 'active'::text,
    CONSTRAINT runtime_route_estimates_direction_id_check CHECK ((direction_id = ANY (ARRAY[0, 1]))),
    CONSTRAINT runtime_route_estimates_status_check CHECK ((status = ANY (ARRAY['active'::text, 'superseded'::text, 'invalid'::text])))
);


--
-- Name: runtime_route_leg_features; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.runtime_route_leg_features (
    leg_feature_id uuid DEFAULT gen_random_uuid() NOT NULL,
    estimate_id uuid NOT NULL,
    leg_idx integer NOT NULL,
    from_seq integer,
    to_seq integer,
    distance_m double precision,
    elev_from_m double precision,
    elev_to_m double precision,
    grade_pct double precision,
    slope_mult double precision,
    slope_bin text,
    signal_count integer,
    offpeak_secs double precision,
    peak_secs double precision,
    offpeak_kmh_effective double precision,
    peak_kmh_effective double precision,
    attrs jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: runtime_route_priors; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.runtime_route_priors (
    id integer NOT NULL,
    route_id uuid NOT NULL,
    route_name text,
    direction_id integer DEFAULT 0,
    time_period text DEFAULT 'all'::text,
    runtime_min_min numeric,
    runtime_typical_min numeric,
    runtime_max_min numeric,
    commercial_kmh numeric,
    source text,
    confidence numeric DEFAULT 0.5,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: runtime_route_priors_id_seq; Type: SEQUENCE; Schema: gtfs_work; Owner: -
--

CREATE SEQUENCE gtfs_work.runtime_route_priors_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: runtime_route_priors_id_seq; Type: SEQUENCE OWNED BY; Schema: gtfs_work; Owner: -
--

ALTER SEQUENCE gtfs_work.runtime_route_priors_id_seq OWNED BY gtfs_work.runtime_route_priors.id;


--
-- Name: service_windows; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.service_windows (
    window_id uuid DEFAULT gen_random_uuid() NOT NULL,
    profile_id uuid NOT NULL,
    start_time text NOT NULL,
    end_time text NOT NULL,
    headway_secs integer,
    exact_departures text[] DEFAULT ARRAY[]::text[] NOT NULL,
    monday boolean DEFAULT true NOT NULL,
    tuesday boolean DEFAULT true NOT NULL,
    wednesday boolean DEFAULT true NOT NULL,
    thursday boolean DEFAULT true NOT NULL,
    friday boolean DEFAULT true NOT NULL,
    saturday boolean DEFAULT false NOT NULL,
    sunday boolean DEFAULT false NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    is_peak boolean DEFAULT false NOT NULL,
    CONSTRAINT service_windows_headway_secs_check CHECK (((headway_secs IS NULL) OR (headway_secs > 0)))
);


--
-- Name: trip_departures; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.trip_departures (
    export_run_id uuid NOT NULL,
    trip_id text NOT NULL,
    departure_time text NOT NULL
);


--
-- Name: upload_runs; Type: TABLE; Schema: gtfs_work; Owner: -
--

CREATE TABLE gtfs_work.upload_runs (
    export_run_id uuid NOT NULL,
    file_name text NOT NULL,
    file_size_bytes bigint,
    uploaded_by text,
    uploaded_at timestamp with time zone DEFAULT now() NOT NULL,
    gtfs_id text
);


--
-- Name: route_semantics; Type: TABLE; Schema: route_prod; Owner: -
--

CREATE TABLE route_prod.route_semantics (
    route_id uuid NOT NULL,
    route_name text NOT NULL,
    route_ref text,
    operator_name text,
    route_aliases text[] DEFAULT ARRAY[]::text[] NOT NULL,
    landmark_tags text[] DEFAULT ARRAY[]::text[] NOT NULL,
    direction_semantics jsonb DEFAULT '{}'::jsonb NOT NULL,
    naming_confidence double precision DEFAULT 0.0 NOT NULL,
    human_verified boolean DEFAULT false NOT NULL,
    semantics_updated_at timestamp with time zone DEFAULT now() NOT NULL,
    search_tsv tsvector,
    model_alias_score double precision,
    service_route_id uuid,
    direction_id smallint,
    version smallint DEFAULT 1,
    pipeline_version text,
    named_by_pipeline_stage text,
    name_approval_at timestamp with time zone,
    CONSTRAINT chk_route_semantics_direction_id CHECK (((direction_id IS NULL) OR (direction_id = ANY (ARRAY[0, 1]))))
);


--
-- Name: routes; Type: TABLE; Schema: route_prod; Owner: -
--

CREATE TABLE route_prod.routes (
    route_id uuid NOT NULL,
    chosen_geometry_candidate_id uuid,
    geom public.geometry(LineString,4326) NOT NULL,
    stop_node_ids uuid[] DEFAULT ARRAY[]::uuid[] NOT NULL,
    source text DEFAULT 'route_constructor'::text NOT NULL,
    route_name text,
    route_aliases text[] DEFAULT ARRAY[]::text[] NOT NULL,
    landmark_tags text[] DEFAULT ARRAY[]::text[] NOT NULL,
    direction_semantics jsonb DEFAULT '{}'::jsonb NOT NULL,
    naming_confidence double precision,
    human_verified boolean DEFAULT false NOT NULL,
    semantics_updated_at timestamp with time zone,
    search_tsv tsvector,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    service_route_id uuid,
    direction_id smallint,
    chosen_stop_sequence_candidate_id uuid,
    canonical_sequence_ready boolean DEFAULT false NOT NULL,
    sequence_approved_at timestamp with time zone,
    sequence_approved_by text,
    province text DEFAULT 'sample_region'::text NOT NULL,
    deploy_status text DEFAULT 'active'::text NOT NULL,
    enrichment_score double precision DEFAULT 0.0 NOT NULL,
    last_enriched_at timestamp with time zone,
    enrichment_history jsonb DEFAULT '[]'::jsonb NOT NULL,
    pending_human_review boolean DEFAULT false NOT NULL,
    source_count smallint DEFAULT 1 NOT NULL,
    inferred_path_polyline text,
    inferred_path_computed_at timestamp with time zone,
    inferred_path_source text,
    version smallint DEFAULT 1 NOT NULL,
    pipeline_version text,
    valhalla_request jsonb,
    geometry_enforcer_report jsonb,
    stop_coverage_report jsonb,
    quality_gate_passed_at timestamp with time zone,
    source_type text DEFAULT 'unknown'::text NOT NULL,
    legacy_grandfathered boolean DEFAULT false NOT NULL,
    grandfathered_until timestamp with time zone,
    approved_by_user uuid,
    approved_at timestamp with time zone,
    last_swap_at timestamp with time zone,
    last_swap_from_version smallint,
    cleanliness_status text,
    dirty_reason text,
    cleanliness_classified_at timestamp with time zone,
    CONSTRAINT chk_cleanliness_consistency CHECK ((((cleanliness_status = 'dirty'::text) AND (dirty_reason IS NOT NULL)) OR ((cleanliness_status = 'clean'::text) AND (dirty_reason IS NULL)) OR ((cleanliness_status = 'unprocessed'::text) AND (dirty_reason IS NULL)) OR ((cleanliness_status IS NULL) AND (dirty_reason IS NULL)))),
    CONSTRAINT chk_cleanliness_status CHECK (((cleanliness_status IS NULL) OR (cleanliness_status = ANY (ARRAY['clean'::text, 'dirty'::text, 'unprocessed'::text])))),
    CONSTRAINT chk_dirty_reason CHECK (((dirty_reason IS NULL) OR (dirty_reason = ANY (ARRAY['real_canon_gap'::text, 'catalog_missing'::text, 'catalog_partial'::text, 'phantom_canon'::text, 'runtime_unbound'::text, 'runtime_bound_stale'::text, 'other'::text])))),
    CONSTRAINT chk_route_prod_direction_id CHECK (((direction_id IS NULL) OR (direction_id = ANY (ARRAY[0, 1])))),
    CONSTRAINT chk_routes_geom_points CHECK ((public.st_npoints(geom) >= 2)),
    CONSTRAINT chk_routes_geom_srid CHECK ((public.st_srid(geom) = 4326)),
    CONSTRAINT chk_routes_geom_type CHECK ((public.geometrytype(geom) = 'LINESTRING'::text)),
    CONSTRAINT routes_approved_timeline CHECK (((approved_at IS NULL) OR (quality_gate_passed_at IS NULL) OR (approved_at <= quality_gate_passed_at))),
    CONSTRAINT routes_audit_trail_required CHECK (((legacy_grandfathered = true) OR ((pipeline_version IS NOT NULL) AND (valhalla_request IS NOT NULL) AND (geometry_enforcer_report IS NOT NULL) AND (stop_coverage_report IS NOT NULL) AND (quality_gate_passed_at IS NOT NULL)))),
    CONSTRAINT routes_grandfathered_has_deadline CHECK (((legacy_grandfathered = false) OR (grandfathered_until IS NOT NULL))),
    CONSTRAINT routes_grandfathered_xor_audit CHECK ((((legacy_grandfathered = true) AND (grandfathered_until IS NOT NULL)) OR ((legacy_grandfathered = false) AND (grandfathered_until IS NULL)))),
    CONSTRAINT routes_inferred_path_source_check CHECK (((inferred_path_source IS NULL) OR (inferred_path_source = ANY (ARRAY['valhalla_terminus_only'::text, 'valhalla_with_anchors'::text, 'osm_relation_geometry'::text])))),
    CONSTRAINT routes_source_type_valid CHECK ((source_type = ANY (ARRAY['constructor_canonical'::text, 'osm_relation_import'::text, 'manual_constructor'::text, 'discovery_pipeline'::text, 'deep_research_override'::text, 'synthetic_fill'::text]))),
    CONSTRAINT routes_version_positive CHECK ((version >= 1))
);


--
-- Name: COLUMN routes.enrichment_score; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.routes.enrichment_score IS 'Progressive enrichment score 0.0-1.0 per continuous_grounding_enrichment.md §1 (5-component formula).';


--
-- Name: COLUMN routes.last_enriched_at; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.routes.last_enriched_at IS 'NULL = never enriched. Updated on every operator-run enrichment merge.';


--
-- Name: COLUMN routes.enrichment_history; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.routes.enrichment_history IS 'Append-only array of enrichment events {timestamp, skill, prior_score, new_score, fields_updated, fields_rejected, evidence_sources, flags}. Flags include terminus_drift_detected, code_disagreement, no_op.';


--
-- Name: COLUMN routes.pending_human_review; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.routes.pending_human_review IS 'TRUE when a confidence-merge resolved to equal-confidence disagreement. Non-overridable passenger-deploy block per hades-quality-gate Soft Rules.';


--
-- Name: COLUMN routes.source_count; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.routes.source_count IS 'Count of distinct research sources that have contributed to this route. Feeds diversity component of enrichment_score.';


--
-- Name: COLUMN routes.inferred_path_polyline; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.routes.inferred_path_polyline IS 'Encoded polyline (Google polyline6 format) of the route path used for synthesis projections. NULL = not computed yet or invalidated.';


--
-- Name: COLUMN routes.inferred_path_computed_at; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.routes.inferred_path_computed_at IS 'When the cached polyline was computed. Invalidate and recompute when new anchors ground, or after 7 days.';


--
-- Name: COLUMN routes.inferred_path_source; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.routes.inferred_path_source IS 'valhalla_terminus_only = only termini fed to Valhalla; valhalla_with_anchors = termini + grounded stops + must_pass_through centroids; osm_relation_geometry = straight from the OSM relation.';


--
-- Name: v_route_inputs; Type: VIEW; Schema: gtfs_work; Owner: -
--

CREATE VIEW gtfs_work.v_route_inputs AS
 SELECT r.route_id,
    COALESCE(s.route_ref, NULL::text) AS route_ref,
    COALESCE(s.route_name, r.route_name, ('route_'::text || "left"((r.route_id)::text, 8))) AS route_name,
    COALESCE(s.operator_name, NULL::text) AS operator_name,
    COALESCE(s.human_verified, false) AS human_verified,
    COALESCE(s.naming_confidence, (0.0)::double precision) AS naming_confidence,
    COALESCE(array_length(r.stop_node_ids, 1), 0) AS n_stops,
    public.st_asewkt(r.geom) AS geom_ewkt,
    r.created_at,
    r.updated_at
   FROM (route_prod.routes r
     LEFT JOIN route_prod.route_semantics s ON ((s.route_id = r.route_id)));


--
-- Name: direction_construction_audit; Type: TABLE; Schema: node_prod; Owner: -
--

CREATE TABLE node_prod.direction_construction_audit (
    id bigint NOT NULL,
    run_id uuid NOT NULL,
    route_id uuid NOT NULL,
    prev_state text,
    new_state text NOT NULL,
    prev_direction_id smallint,
    new_direction_id smallint,
    paired_route_id uuid,
    synthesized_node_ids uuid[],
    pair_score real,
    source text NOT NULL,
    reason text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: direction_construction_audit_id_seq; Type: SEQUENCE; Schema: node_prod; Owner: -
--

CREATE SEQUENCE node_prod.direction_construction_audit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: direction_construction_audit_id_seq; Type: SEQUENCE OWNED BY; Schema: node_prod; Owner: -
--

ALTER SEQUENCE node_prod.direction_construction_audit_id_seq OWNED BY node_prod.direction_construction_audit.id;


--
-- Name: precision_snap_audit; Type: TABLE; Schema: node_prod; Owner: -
--

CREATE TABLE node_prod.precision_snap_audit (
    id bigint NOT NULL,
    run_id uuid NOT NULL,
    node_id uuid NOT NULL,
    route_id uuid NOT NULL,
    direction smallint NOT NULL,
    stop_sequence integer,
    orig_lat double precision,
    orig_lng double precision,
    new_lat double precision,
    new_lng double precision,
    shift_m double precision,
    arc_length_s double precision,
    source_type text,
    confidence real,
    action text NOT NULL,
    reason text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: precision_snap_audit_id_seq; Type: SEQUENCE; Schema: node_prod; Owner: -
--

CREATE SEQUENCE node_prod.precision_snap_audit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: precision_snap_audit_id_seq; Type: SEQUENCE OWNED BY; Schema: node_prod; Owner: -
--

ALTER SEQUENCE node_prod.precision_snap_audit_id_seq OWNED BY node_prod.precision_snap_audit.id;


--
-- Name: stop_treatment_log; Type: TABLE; Schema: node_prod; Owner: -
--

CREATE TABLE node_prod.stop_treatment_log (
    id bigint NOT NULL,
    treatment_id uuid DEFAULT gen_random_uuid() NOT NULL,
    node_id uuid,
    operation text NOT NULL,
    caller text NOT NULL,
    name_before text,
    name_after text,
    name_was_forbidden boolean,
    name_normalized boolean,
    context_name_applied boolean,
    context_name_method text,
    coord_before_lat numeric,
    coord_before_lon numeric,
    coord_after_lat numeric,
    coord_after_lon numeric,
    coord_changed boolean,
    place_id uuid,
    place_mapping_created boolean,
    place_mapping_validated boolean,
    success boolean NOT NULL,
    error_reason text,
    treated_at timestamp with time zone DEFAULT now() NOT NULL,
    treated_by text,
    transaction_id bigint,
    CONSTRAINT stop_treatment_log_operation_check CHECK ((operation = ANY (ARRAY['synthetic_insert'::text, 'name_repair'::text, 'snap_align'::text, 'refill_adopt'::text, 'ground_validate'::text, 'cover_validate'::text, 'phase3_end_audit'::text, 'approve_promote'::text])))
);


--
-- Name: stop_treatment_log_id_seq; Type: SEQUENCE; Schema: node_prod; Owner: -
--

CREATE SEQUENCE node_prod.stop_treatment_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: stop_treatment_log_id_seq; Type: SEQUENCE OWNED BY; Schema: node_prod; Owner: -
--

ALTER SEQUENCE node_prod.stop_treatment_log_id_seq OWNED BY node_prod.stop_treatment_log.id;


--
-- Name: synthesis_events; Type: TABLE; Schema: node_prod; Owner: -
--

CREATE TABLE node_prod.synthesis_events (
    id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    node_id uuid,
    route_id uuid,
    unit text NOT NULL,
    province text NOT NULL,
    stage text NOT NULL,
    anchor_name text,
    research_coords_lat real,
    research_coords_lon real,
    final_coords_lat real,
    final_coords_lon real,
    match_score real,
    rejected_reason text,
    triggered_by_skill text,
    research_output_file text,
    CONSTRAINT synthesis_events_stage_check CHECK ((stage = ANY (ARRAY['3a_poi_on_path'::text, '3b_path_corridor'::text, '3c_path_intersection'::text, '3d_research_coords_snapped'::text, '4_pure_synthesis'::text, 'osm_route_fill'::text])))
);


--
-- Name: TABLE synthesis_events; Type: COMMENT; Schema: node_prod; Owner: -
--

COMMENT ON TABLE node_prod.synthesis_events IS 'Append-only audit log of every synthesis attempt (successful or rejected). Retention: indefinite. Never UPDATE, never DELETE.';


--
-- Name: COLUMN synthesis_events.node_id; Type: COMMENT; Schema: node_prod; Owner: -
--

COMMENT ON COLUMN node_prod.synthesis_events.node_id IS 'FK to the created synthetic node. NULL if the attempt was rejected (no node created) or if the node was later hard-deleted (preserved for audit).';


--
-- Name: COLUMN synthesis_events.stage; Type: COMMENT; Schema: node_prod; Owner: -
--

COMMENT ON COLUMN node_prod.synthesis_events.stage IS 'Which pipeline stage produced this event. See hades-path-aware-synthesis §5 for 3a-3d + stage 4, and hades-osm-route-node-fill for osm_route_fill.';


--
-- Name: COLUMN synthesis_events.research_output_file; Type: COMMENT; Schema: node_prod; Owner: -
--

COMMENT ON COLUMN node_prod.synthesis_events.research_output_file IS 'Filename under workspace/research_queue/responses/ or ingested/ that triggered this synthesis (foreign-key-by-filename per 07_INGESTION_CONTRACT).';


--
-- Name: synthesis_events_id_seq; Type: SEQUENCE; Schema: node_prod; Owner: -
--

CREATE SEQUENCE node_prod.synthesis_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: synthesis_events_id_seq; Type: SEQUENCE OWNED BY; Schema: node_prod; Owner: -
--

ALTER SEQUENCE node_prod.synthesis_events_id_seq OWNED BY node_prod.synthesis_events.id;


--
-- Name: synthetic_osm_id_seq; Type: SEQUENCE; Schema: node_prod; Owner: -
--

CREATE SEQUENCE node_prod.synthetic_osm_id_seq
    START WITH -1000000
    INCREMENT BY -1
    MINVALUE -9223372036854775807
    MAXVALUE -1000000
    CACHE 100;


--
-- Name: SEQUENCE synthetic_osm_id_seq; Type: COMMENT; Schema: node_prod; Owner: -
--

COMMENT ON SEQUENCE node_prod.synthetic_osm_id_seq IS 'Negative bigint sequence for synthetic osm_id. Callers: SELECT nextval(''node_prod.synthetic_osm_id_seq''). First real allocation returns -1000000.';


--
-- Name: v_nodes_quality; Type: VIEW; Schema: node_prod; Owner: -
--

CREATE VIEW node_prod.v_nodes_quality AS
 SELECT node_id,
    geom,
    node_type,
    name,
    ref,
    operator,
    tag_kind,
    source,
    source_node_set_id,
    chosen_candidate_id,
    chosen_tags,
    confidence,
    approved_at,
    updated_at,
        CASE
            WHEN (confidence >= (0.7)::double precision) THEN 'HIGH'::text
            WHEN (confidence >= (0.4)::double precision) THEN 'MEDIUM'::text
            ELSE 'LOW'::text
        END AS quality_tier
   FROM node_prod.nodes;


--
-- Name: overpass_actions; Type: TABLE; Schema: node_raw; Owner: -
--

CREATE TABLE node_raw.overpass_actions (
    action_id text NOT NULL,
    template_path text NOT NULL,
    default_params jsonb DEFAULT '{}'::jsonb NOT NULL,
    outputs text[] DEFAULT ARRAY[]::text[] NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: overpass_elements; Type: TABLE; Schema: node_raw; Owner: -
--

CREATE TABLE node_raw.overpass_elements (
    run_id uuid NOT NULL,
    osm_type text NOT NULL,
    osm_id bigint NOT NULL,
    lat double precision,
    lon double precision,
    center_lat double precision,
    center_lon double precision,
    tags jsonb DEFAULT '{}'::jsonb NOT NULL,
    geom public.geometry(Point,4326),
    inserted_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT chk_node_overpass_elements_osm_type CHECK ((osm_type = ANY (ARRAY['node'::text, 'way'::text, 'relation'::text])))
);


--
-- Name: overpass_queries; Type: TABLE; Schema: node_raw; Owner: -
--

CREATE TABLE node_raw.overpass_queries (
    query_id uuid DEFAULT gen_random_uuid() NOT NULL,
    action_id text NOT NULL,
    query_text text NOT NULL,
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: overpass_runs; Type: TABLE; Schema: node_raw; Owner: -
--

CREATE TABLE node_raw.overpass_runs (
    run_id uuid DEFAULT gen_random_uuid() NOT NULL,
    query_id uuid NOT NULL,
    bbox jsonb,
    area_id text,
    status text NOT NULL,
    runtime_ms integer,
    element_count integer,
    response_bytes integer,
    fetched_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT chk_node_overpass_runs_status CHECK ((status = ANY (ARRAY['ok'::text, 'timeout'::text, 'error'::text])))
);


--
-- Name: bandit_state; Type: TABLE; Schema: node_work; Owner: -
--

CREATE TABLE node_work.bandit_state (
    key text NOT NULL,
    state jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: mv_road_lines; Type: MATERIALIZED VIEW; Schema: node_work; Owner: -
--

CREATE MATERIALIZED VIEW node_work.mv_road_lines AS
 SELECT osm_id,
    geom,
    (tags ->> 'highway'::text) AS road_type
   FROM node_raw.overpass_elements oe
  WHERE ((osm_type = 'way'::text) AND ((tags ->> 'highway'::text) IS NOT NULL) AND (geom IS NOT NULL))
  WITH NO DATA;


--
-- Name: node_candidate_sets; Type: TABLE; Schema: node_work; Owner: -
--

CREATE TABLE node_work.node_candidate_sets (
    node_set_id uuid DEFAULT gen_random_uuid() NOT NULL,
    source_run_ids uuid[] NOT NULL,
    action_ids text[] DEFAULT ARRAY[]::text[] NOT NULL,
    params_used jsonb DEFAULT '{}'::jsonb NOT NULL,
    rank_score double precision,
    rank_model_ver text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: node_candidates; Type: TABLE; Schema: node_work; Owner: -
--

CREATE TABLE node_work.node_candidates (
    node_candidate_id uuid DEFAULT gen_random_uuid() NOT NULL,
    node_set_id uuid NOT NULL,
    source_run_id uuid NOT NULL,
    osm_type text NOT NULL,
    osm_id bigint NOT NULL,
    geom public.geometry(Point,4326) NOT NULL,
    tags jsonb DEFAULT '{}'::jsonb NOT NULL,
    tag_kind text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT chk_node_candidates_osm_type CHECK ((osm_type = ANY (ARRAY['node'::text, 'way'::text, 'relation'::text])))
);


--
-- Name: node_clusters; Type: TABLE; Schema: node_work; Owner: -
--

CREATE TABLE node_work.node_clusters (
    node_set_id uuid NOT NULL,
    node_candidate_id uuid NOT NULL,
    cluster_id uuid NOT NULL,
    eps_m integer NOT NULL,
    min_pts integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: node_features; Type: TABLE; Schema: node_work; Owner: -
--

CREATE TABLE node_work.node_features (
    node_candidate_id uuid NOT NULL,
    has_name boolean DEFAULT false NOT NULL,
    has_ref boolean DEFAULT false NOT NULL,
    has_operator boolean DEFAULT false NOT NULL,
    confidence_v0 double precision DEFAULT 0.0 NOT NULL,
    model_version text,
    prob_stop double precision,
    prob_poi double precision,
    node_class_pred text,
    computed_at timestamp with time zone DEFAULT now() NOT NULL,
    has_shelter boolean DEFAULT false,
    has_bench boolean DEFAULT false,
    has_route_ref boolean DEFAULT false,
    primary_tag_category text DEFAULT 'UNKNOWN'::text,
    tag_richness integer DEFAULT 0,
    distance_to_nearest_road_m double precision,
    distance_to_nearest_stop_m double precision,
    nearby_stop_density_100m integer DEFAULT 0,
    nearby_poi_density_100m integer DEFAULT 0,
    road_type_nearest text,
    on_road_way boolean DEFAULT false,
    CONSTRAINT node_features_node_class_pred_check CHECK ((node_class_pred = ANY (ARRAY['STOP'::text, 'POI'::text])))
);


--
-- Name: node_review_requests; Type: TABLE; Schema: node_work; Owner: -
--

CREATE TABLE node_work.node_review_requests (
    request_id uuid DEFAULT gen_random_uuid() NOT NULL,
    source text DEFAULT 'free_create'::text NOT NULL,
    route_id uuid,
    seq integer,
    status text DEFAULT 'requested'::text NOT NULL,
    lat double precision NOT NULL,
    lon double precision NOT NULL,
    node_type text DEFAULT 'STOP'::text NOT NULL,
    name text,
    ref text,
    operator text,
    tags jsonb DEFAULT '{}'::jsonb NOT NULL,
    requested_by text,
    reviewed_by text,
    reviewed_at timestamp with time zone,
    approved_node_id uuid,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT chk_node_review_requests_source CHECK ((source = ANY (ARRAY['phase3_route'::text, 'free_create'::text]))),
    CONSTRAINT chk_node_review_requests_status CHECK ((status = ANY (ARRAY['requested'::text, 'approved'::text, 'rejected'::text]))),
    CONSTRAINT chk_node_review_requests_type CHECK ((node_type = ANY (ARRAY['STOP'::text, 'POI'::text])))
);


--
-- Name: nodes_resolved; Type: TABLE; Schema: node_work; Owner: -
--

CREATE TABLE node_work.nodes_resolved (
    node_id uuid DEFAULT gen_random_uuid() NOT NULL,
    node_set_id uuid NOT NULL,
    cluster_id uuid NOT NULL,
    geom public.geometry(Point,4326) NOT NULL,
    chosen_candidate_id uuid NOT NULL,
    chosen_tags jsonb DEFAULT '{}'::jsonb NOT NULL,
    confidence double precision DEFAULT 0.0 NOT NULL,
    status text DEFAULT 'work'::text NOT NULL,
    resolved_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT nodes_resolved_status_check CHECK ((status = ANY (ARRAY['work'::text, 'approved'::text, 'rejected'::text])))
);


--
-- Name: v_node_sets; Type: VIEW; Schema: node_work; Owner: -
--

CREATE VIEW node_work.v_node_sets AS
 WITH agg AS (
         SELECT r.node_set_id,
            count(*) AS n_resolved,
            max(r.resolved_at) AS resolved_at_max,
            sum(
                CASE
                    WHEN (r.status = 'approved'::text) THEN 1
                    ELSE 0
                END) AS n_approved,
            sum(
                CASE
                    WHEN (r.status = 'work'::text) THEN 1
                    ELSE 0
                END) AS n_work,
            sum(
                CASE
                    WHEN (r.status = 'rejected'::text) THEN 1
                    ELSE 0
                END) AS n_rejected
           FROM node_work.nodes_resolved r
          GROUP BY r.node_set_id
        )
 SELECT s.node_set_id,
    s.created_at,
    COALESCE(a.resolved_at_max, NULL::timestamp with time zone) AS resolved_at,
    COALESCE(a.n_resolved, (0)::bigint) AS n_resolved,
        CASE
            WHEN ((a.node_set_id IS NULL) OR (a.n_resolved = 0)) THEN 'empty'::text
            WHEN (a.n_rejected > 0) THEN 'rejected'::text
            WHEN (a.n_work > 0) THEN 'work'::text
            ELSE 'approved'::text
        END AS status,
    COALESCE(a.n_approved, (0)::bigint) AS n_approved,
    COALESCE(a.n_work, (0)::bigint) AS n_work,
    COALESCE(a.n_rejected, (0)::bigint) AS n_rejected
   FROM (node_work.node_candidate_sets s
     LEFT JOIN agg a ON ((a.node_set_id = s.node_set_id)))
  ORDER BY s.created_at DESC;


--
-- Name: v_raw_to_node_candidate; Type: VIEW; Schema: node_work; Owner: -
--

CREATE VIEW node_work.v_raw_to_node_candidate AS
 SELECT run_id AS source_run_id,
    osm_type,
    osm_id,
    COALESCE(geom,
        CASE
            WHEN ((lon IS NOT NULL) AND (lat IS NOT NULL)) THEN public.st_setsrid(public.st_makepoint(lon, lat), 4326)
            WHEN ((center_lon IS NOT NULL) AND (center_lat IS NOT NULL)) THEN public.st_setsrid(public.st_makepoint(center_lon, center_lat), 4326)
            ELSE NULL::public.geometry
        END) AS geom,
    tags,
        CASE
            WHEN ((tags ? 'highway'::text) AND ((tags ->> 'highway'::text) = 'bus_stop'::text)) THEN 'bus_stop'::text
            WHEN ((tags ? 'public_transport'::text) AND ((tags ->> 'public_transport'::text) = 'platform'::text)) THEN 'platform'::text
            WHEN ((tags ? 'public_transport'::text) AND ((tags ->> 'public_transport'::text) = 'stop_position'::text)) THEN 'stop_position'::text
            WHEN ((tags ? 'amenity'::text) AND ((tags ->> 'amenity'::text) = 'bus_station'::text)) THEN 'station'::text
            WHEN ((tags ? 'railway'::text) AND ((tags ->> 'railway'::text) = 'tram_stop'::text)) THEN 'tram_stop'::text
            ELSE 'other'::text
        END AS tag_kind
   FROM node_raw.overpass_elements e;


--
-- Name: approval_queue; Type: TABLE; Schema: route_prod; Owner: -
--

CREATE TABLE route_prod.approval_queue (
    queue_id uuid DEFAULT gen_random_uuid() NOT NULL,
    route_code text NOT NULL,
    version smallint DEFAULT 1 NOT NULL,
    enqueued_at timestamp with time zone DEFAULT now() NOT NULL,
    policy_profile text NOT NULL,
    decision_reasons jsonb NOT NULL,
    policy_flags jsonb DEFAULT '{}'::jsonb NOT NULL,
    geometry_report jsonb NOT NULL,
    stop_coverage_report jsonb NOT NULL,
    proposed_stops jsonb NOT NULL,
    proposed_shape jsonb NOT NULL,
    dr_queries_queued jsonb DEFAULT '[]'::jsonb NOT NULL,
    dr_queries_deferred jsonb DEFAULT '[]'::jsonb NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    resolved_at timestamp with time zone,
    resolved_by text,
    resolution_notes text,
    crashed boolean DEFAULT false NOT NULL,
    crash_payload jsonb,
    quality_class text,
    tier4_pending_count smallint DEFAULT 0 NOT NULL,
    pending_dr_batches text[] DEFAULT '{}'::text[] NOT NULL,
    classified_at timestamp with time zone,
    reclassified_count smallint DEFAULT 0 NOT NULL,
    pre_ship_cleanup_applied boolean DEFAULT false NOT NULL,
    pre_ship_cleanup_report jsonb,
    pre_ship_cleanup_at timestamp with time zone,
    proposed_refill_candidates jsonb,
    refill_decisions jsonb,
    refill_applied boolean DEFAULT false NOT NULL,
    refill_at timestamp with time zone,
    CONSTRAINT approval_queue_policy_profile_check CHECK ((policy_profile = ANY (ARRAY['conservative'::text, 'balanced'::text, 'aggressive_supervised'::text]))),
    CONSTRAINT approval_queue_quality_class_check CHECK (((quality_class IS NULL) OR (quality_class = ANY (ARRAY['good'::text, 'acceptable'::text, 'ship_pending_dr'::text, 'degraded_minor'::text, 'degraded'::text, 'unroutable'::text, 'unknown'::text])))),
    CONSTRAINT approval_queue_resolved_consistency CHECK (((status = 'pending'::text) OR ((status = ANY (ARRAY['approved'::text, 'rejected'::text, 'sent_to_phase2'::text])) AND (resolved_at IS NOT NULL)))),
    CONSTRAINT approval_queue_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text, 'sent_to_phase2'::text])))
);


--
-- Name: TABLE approval_queue; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON TABLE route_prod.approval_queue IS 'Phase 3 enforcer + policy engine approval queue. Satellite to route_prod.routes; queued routes are not promoted.';


--
-- Name: COLUMN approval_queue.decision_reasons; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.approval_queue.decision_reasons IS 'Human-readable reason strings from hades.enforcers.policy_engine.';


--
-- Name: COLUMN approval_queue.proposed_stops; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.approval_queue.proposed_stops IS 'Stops as they would land in route_prod.routes.stop_node_ids after enforcer enhancement.';


--
-- Name: COLUMN approval_queue.dr_queries_queued; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.approval_queue.dr_queries_queued IS 'DR Type 2 queries the coordinator successfully booked against the unit budget for this route.';


--
-- Name: COLUMN approval_queue.dr_queries_deferred; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.approval_queue.dr_queries_deferred IS 'DR queries not booked — carries per-query deferred_reason (zone / gap / budget).';


--
-- Name: COLUMN approval_queue.quality_class; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.approval_queue.quality_class IS 'v2 quality taxonomy assigned by hades.enforcers.stop_coverage_enforcer._classify. NULL = not yet classified by the v2 path.';


--
-- Name: COLUMN approval_queue.tier4_pending_count; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.approval_queue.tier4_pending_count IS 'Count of unresolved tier-4 (DR-prepared) gaps in this v2. Drives the ship_pending_dr 8-cap rule.';


--
-- Name: COLUMN approval_queue.pending_dr_batches; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.approval_queue.pending_dr_batches IS 'DR batch ids whose successful landing would unblock at least one tier-4 gap on this v2. Populated by populate_dr_dependencies.';


--
-- Name: COLUMN approval_queue.classified_at; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.approval_queue.classified_at IS 'Wall-clock time the classifier last ran. Surfaced in the dashboard header ("last reclassified ...").';


--
-- Name: COLUMN approval_queue.reclassified_count; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.approval_queue.reclassified_count IS 'Counter, incremented by hades.enforcers.reclassifier each time it re-runs the classifier on this row.';


--
-- Name: COLUMN approval_queue.pre_ship_cleanup_applied; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.approval_queue.pre_ship_cleanup_applied IS 'TRUE iff hades.enforcers.orphan_cleanup applied to this row and the safety gate accepted the result. FALSE for not-yet-attempted or rejected cleanups.';


--
-- Name: COLUMN approval_queue.pre_ship_cleanup_report; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.approval_queue.pre_ship_cleanup_report IS 'OrphanCleanupReport serialized as JSONB — counts, projections, removed orphans, thresholds. NULL when cleanup has not run.';


--
-- Name: COLUMN approval_queue.pre_ship_cleanup_at; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.approval_queue.pre_ship_cleanup_at IS 'Timestamp when cleanup was applied (set together with pre_ship_cleanup_applied=TRUE).';


--
-- Name: COLUMN approval_queue.proposed_refill_candidates; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.approval_queue.proposed_refill_candidates IS 'List of RefillCandidate dicts produced by hades.enforcers.stop_refill at preview time. NULL until preview runs.';


--
-- Name: COLUMN approval_queue.refill_decisions; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.approval_queue.refill_decisions IS 'Map of stop_id → operator decision ("accepted" | "skipped" | "reviewed_later"). NULL until at least one decision recorded.';


--
-- Name: COLUMN approval_queue.refill_applied; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.approval_queue.refill_applied IS 'TRUE iff the swap was confirmed AND at least one refill candidate was accepted into v2.';


--
-- Name: COLUMN approval_queue.refill_at; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.approval_queue.refill_at IS 'Timestamp when refill decisions were applied (set together with refill_applied=TRUE).';


--
-- Name: cleanup_override_audit; Type: TABLE; Schema: route_prod; Owner: -
--

CREATE TABLE route_prod.cleanup_override_audit (
    id bigint NOT NULL,
    queue_id uuid NOT NULL,
    route_code text NOT NULL,
    override_reason text NOT NULL,
    override_by text,
    override_at timestamp with time zone DEFAULT now() NOT NULL,
    cleanup_report_at_override jsonb
);


--
-- Name: cleanup_override_audit_id_seq; Type: SEQUENCE; Schema: route_prod; Owner: -
--

CREATE SEQUENCE route_prod.cleanup_override_audit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: cleanup_override_audit_id_seq; Type: SEQUENCE OWNED BY; Schema: route_prod; Owner: -
--

ALTER SEQUENCE route_prod.cleanup_override_audit_id_seq OWNED BY route_prod.cleanup_override_audit.id;


--
-- Name: cleanup_v2_to_postapproval_resets; Type: TABLE; Schema: route_prod; Owner: -
--

CREATE TABLE route_prod.cleanup_v2_to_postapproval_resets (
    id bigint NOT NULL,
    queue_id uuid NOT NULL,
    route_code text NOT NULL,
    reset_at timestamp with time zone DEFAULT now() NOT NULL,
    prior_cleanup_report jsonb
);


--
-- Name: cleanup_v2_to_postapproval_resets_id_seq; Type: SEQUENCE; Schema: route_prod; Owner: -
--

CREATE SEQUENCE route_prod.cleanup_v2_to_postapproval_resets_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: cleanup_v2_to_postapproval_resets_id_seq; Type: SEQUENCE OWNED BY; Schema: route_prod; Owner: -
--

ALTER SEQUENCE route_prod.cleanup_v2_to_postapproval_resets_id_seq OWNED BY route_prod.cleanup_v2_to_postapproval_resets.id;


--
-- Name: dr_batch_dependencies; Type: TABLE; Schema: route_prod; Owner: -
--

CREATE TABLE route_prod.dr_batch_dependencies (
    dependency_id uuid DEFAULT gen_random_uuid() NOT NULL,
    dr_batch_id text NOT NULL,
    route_id uuid NOT NULL,
    gap_idx smallint NOT NULL,
    gap_coords jsonb,
    status text DEFAULT 'waiting'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_at timestamp with time zone,
    CONSTRAINT dr_batch_dependencies_status_check CHECK ((status = ANY (ARRAY['waiting'::text, 'batch_processed'::text, 'landmark_found'::text, 'landmark_not_found'::text])))
);


--
-- Name: TABLE dr_batch_dependencies; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON TABLE route_prod.dr_batch_dependencies IS 'Per-gap predictions of which DR batch will unblock which approval_queue row. Populated by hades.enforcers.reclassifier.populate_dr_dependencies; consumed by the reclassifier when batches land.';


--
-- Name: COLUMN dr_batch_dependencies.dr_batch_id; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.dr_batch_dependencies.dr_batch_id IS 'Existing multi-route bundle id (e.g. "batch_12_sangolqui_rumi") OR a unit-prefixed on-demand id (e.g. "qc_007"). Both formats coexist by design.';


--
-- Name: COLUMN dr_batch_dependencies.gap_coords; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON COLUMN route_prod.dr_batch_dependencies.gap_coords IS 'Gap midpoint as {"lat": <decimal>, "lon": <decimal>}. Used by the dashboard to render the dependency without re-reading stop_coverage_report.';


--
-- Name: fix_reports; Type: TABLE; Schema: route_prod; Owner: -
--

CREATE TABLE route_prod.fix_reports (
    report_id uuid DEFAULT gen_random_uuid() NOT NULL,
    route_id uuid NOT NULL,
    version_before smallint NOT NULL,
    version_after smallint NOT NULL,
    fix_category text NOT NULL,
    fixes_applied jsonb NOT NULL,
    metrics_before jsonb NOT NULL,
    metrics_after jsonb NOT NULL,
    applied_at timestamp with time zone DEFAULT now() NOT NULL,
    applied_by text,
    CONSTRAINT chk_fix_reports_version_bump CHECK ((version_after > version_before)),
    CONSTRAINT fix_reports_fix_category_check CHECK ((fix_category = ANY (ARRAY['geometry'::text, 'stop_coverage'::text, 'structural'::text])))
);


--
-- Name: re_entry_queue; Type: TABLE; Schema: route_prod; Owner: -
--

CREATE TABLE route_prod.re_entry_queue (
    queue_id uuid DEFAULT gen_random_uuid() NOT NULL,
    route_id uuid NOT NULL,
    current_version smallint NOT NULL,
    priority integer NOT NULL,
    classification text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    priority_reason text,
    scheduled_at timestamp with time zone,
    attempted_at timestamp with time zone,
    last_error text,
    attempts smallint DEFAULT 0 NOT NULL,
    enqueued_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_at timestamp with time zone,
    CONSTRAINT re_entry_queue_classification_check CHECK ((classification = ANY (ARRAY['osm_relation_severe'::text, 'osm_relation_clean'::text, 'discovery_legacy'::text, 'constructor_canonical_legacy'::text, 'manual_constructor_legacy'::text, 'structural_repair'::text, 'deep_research_legacy'::text, 'unclassified'::text]))),
    CONSTRAINT re_entry_queue_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'in_progress'::text, 'v2_ready'::text, 'approved'::text, 'swapped'::text, 'failed'::text, 'quarantined'::text])))
);


--
-- Name: refill_audit; Type: TABLE; Schema: route_prod; Owner: -
--

CREATE TABLE route_prod.refill_audit (
    id bigint NOT NULL,
    queue_id uuid NOT NULL,
    route_code text NOT NULL,
    candidate_stop_id uuid NOT NULL,
    decision text NOT NULL,
    decided_at timestamp with time zone DEFAULT now() NOT NULL,
    decided_by text,
    candidate_score numeric,
    candidate_metadata jsonb,
    CONSTRAINT refill_audit_decision_check CHECK ((decision = ANY (ARRAY['accepted'::text, 'skipped'::text, 'reviewed_later'::text])))
);


--
-- Name: TABLE refill_audit; Type: COMMENT; Schema: route_prod; Owner: -
--

COMMENT ON TABLE route_prod.refill_audit IS 'One row per operator decision on a refill candidate. Lets us replay why a stop was/was not refilled into a given route, and analyse algorithm acceptance over time.';


--
-- Name: refill_audit_id_seq; Type: SEQUENCE; Schema: route_prod; Owner: -
--

CREATE SEQUENCE route_prod.refill_audit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: refill_audit_id_seq; Type: SEQUENCE OWNED BY; Schema: route_prod; Owner: -
--

ALTER SEQUENCE route_prod.refill_audit_id_seq OWNED BY route_prod.refill_audit.id;


--
-- Name: routes_audit; Type: TABLE; Schema: route_prod; Owner: -
--

CREATE TABLE route_prod.routes_audit (
    audit_id bigint NOT NULL,
    route_id uuid NOT NULL,
    version smallint,
    action text NOT NULL,
    changed_columns jsonb,
    approved_by uuid,
    approval_reason text,
    trigger_function text,
    event_at timestamp with time zone DEFAULT now() NOT NULL,
    application_name text,
    "session_user" text,
    CONSTRAINT routes_audit_action_check CHECK ((action = ANY (ARRAY['INSERT'::text, 'UPDATE'::text, 'DELETE'::text, 'REJECT'::text])))
);


--
-- Name: routes_audit_audit_id_seq; Type: SEQUENCE; Schema: route_prod; Owner: -
--

CREATE SEQUENCE route_prod.routes_audit_audit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: routes_audit_audit_id_seq; Type: SEQUENCE OWNED BY; Schema: route_prod; Owner: -
--

ALTER SEQUENCE route_prod.routes_audit_audit_id_seq OWNED BY route_prod.routes_audit.audit_id;


--
-- Name: route_jobs; Type: TABLE; Schema: route_raw; Owner: -
--

CREATE TABLE route_raw.route_jobs (
    route_id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text,
    status text DEFAULT 'new'::text NOT NULL,
    notes text,
    area_key text,
    bbox jsonb,
    known_ref text,
    chosen_osm_relation_id bigint,
    service_route_id uuid,
    direction_id smallint,
    extractor_source text,
    extractor_review jsonb,
    is_trashed boolean DEFAULT false NOT NULL,
    trashed_at timestamp with time zone,
    trash_id uuid,
    province text DEFAULT 'sample_region'::text NOT NULL,
    CONSTRAINT chk_route_jobs_direction_id CHECK (((direction_id IS NULL) OR (direction_id = ANY (ARRAY[0, 1]))))
);


--
-- Name: active_route_jobs; Type: VIEW; Schema: route_raw; Owner: -
--

CREATE VIEW route_raw.active_route_jobs AS
 SELECT route_id,
    created_at,
    created_by,
    status,
    notes,
    area_key,
    bbox,
    known_ref,
    chosen_osm_relation_id,
    service_route_id,
    direction_id,
    extractor_source,
    extractor_review,
    is_trashed,
    trashed_at,
    trash_id,
    province
   FROM route_raw.route_jobs
  WHERE (is_trashed = false);


--
-- Name: osm_relations_raw; Type: TABLE; Schema: route_raw; Owner: -
--

CREATE TABLE route_raw.osm_relations_raw (
    route_id uuid NOT NULL,
    osm_relation_id bigint NOT NULL,
    fetched_at timestamp with time zone DEFAULT now() NOT NULL,
    overpass_json jsonb NOT NULL,
    overpass_query text,
    overpass_url text,
    http_status integer,
    response_ms integer
);


--
-- Name: relation_candidates; Type: TABLE; Schema: route_raw; Owner: -
--

CREATE TABLE route_raw.relation_candidates (
    candidate_id uuid DEFAULT gen_random_uuid() NOT NULL,
    route_id uuid NOT NULL,
    osm_relation_id bigint NOT NULL,
    rel_type text,
    route_mode text,
    ref text,
    name text,
    operator text,
    tags jsonb DEFAULT '{}'::jsonb NOT NULL,
    found_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: service_route_directions; Type: TABLE; Schema: route_raw; Owner: -
--

CREATE TABLE route_raw.service_route_directions (
    service_route_id uuid NOT NULL,
    direction_id smallint NOT NULL,
    route_id uuid,
    phase3_progress_step integer DEFAULT 0 NOT NULL,
    progress_notes text,
    direction_approval_status text DEFAULT 'pending'::text NOT NULL,
    geom_source text DEFAULT 'unknown'::text NOT NULL,
    approved_at timestamp with time zone,
    approved_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT chk_service_route_directions_direction_approval_status CHECK ((direction_approval_status = ANY (ARRAY['pending'::text, 'in_progress'::text, 'ready'::text, 'approved'::text, 'rejected'::text]))),
    CONSTRAINT chk_service_route_directions_geom_source CHECK ((geom_source = ANY (ARRAY['unknown'::text, 'observed'::text, 'reversed'::text, 'inferred'::text, 'manual'::text]))),
    CONSTRAINT chk_service_route_directions_progress_step CHECK (((phase3_progress_step >= 0) AND (phase3_progress_step <= 40))),
    CONSTRAINT service_route_directions_direction_approval_status_check CHECK ((direction_approval_status = ANY (ARRAY['pending'::text, 'in_progress'::text, 'ready'::text, 'approved'::text, 'rejected'::text]))),
    CONSTRAINT service_route_directions_direction_id_check CHECK ((direction_id = ANY (ARRAY[0, 1]))),
    CONSTRAINT service_route_directions_geom_source_check CHECK ((geom_source = ANY (ARRAY['unknown'::text, 'observed'::text, 'reversed'::text, 'inferred'::text, 'manual'::text]))),
    CONSTRAINT service_route_directions_phase3_progress_step_check CHECK (((phase3_progress_step >= 0) AND (phase3_progress_step <= 40)))
);


--
-- Name: service_routes; Type: TABLE; Schema: route_raw; Owner: -
--

CREATE TABLE route_raw.service_routes (
    service_route_id uuid DEFAULT gen_random_uuid() NOT NULL,
    route_ref text,
    route_name text,
    operator_name text,
    created_by text,
    notes text,
    route_approval_status text DEFAULT 'pending'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT service_routes_route_approval_status_check CHECK ((route_approval_status = ANY (ARRAY['pending'::text, 'in_progress'::text, 'ready'::text, 'approved'::text, 'rejected'::text])))
);


--
-- Name: coverage_gaps; Type: TABLE; Schema: route_review; Owner: -
--

CREATE TABLE route_review.coverage_gaps (
    gap_id uuid DEFAULT gen_random_uuid() NOT NULL,
    dedupe_key text NOT NULL,
    source_catalog text NOT NULL,
    sector_key text NOT NULL,
    sector_label text,
    route_family_hint text NOT NULL,
    known_aliases text[] DEFAULT ARRAY[]::text[] NOT NULL,
    start_hint text,
    end_hint text,
    direction_hint text,
    evidence_summary jsonb DEFAULT '{}'::jsonb NOT NULL,
    related_route_ids uuid[] DEFAULT ARRAY[]::uuid[] NOT NULL,
    classification_status text DEFAULT 'needs_review'::text NOT NULL,
    classification_confidence double precision,
    classification_source text DEFAULT 'system'::text NOT NULL,
    operator_override_classification text,
    manual_priority text DEFAULT 'medium'::text NOT NULL,
    recommended_next_action text,
    heuristic_notes jsonb DEFAULT '{}'::jsonb NOT NULL,
    resolution_status text DEFAULT 'open'::text NOT NULL,
    resolved_route_id uuid,
    resolved_prod_route_id uuid,
    reviewed_at timestamp with time zone,
    reviewed_by text,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT coverage_gaps_classification_status_check CHECK ((classification_status = ANY (ARRAY['needs_review'::text, 'still_extractable'::text, 'non_reliably_extractable'::text]))),
    CONSTRAINT coverage_gaps_manual_priority_check CHECK ((manual_priority = ANY (ARRAY['low'::text, 'medium'::text, 'high'::text, 'critical'::text]))),
    CONSTRAINT coverage_gaps_operator_override_classification_check CHECK ((operator_override_classification = ANY (ARRAY['still_extractable'::text, 'non_reliably_extractable'::text]))),
    CONSTRAINT coverage_gaps_resolution_status_check CHECK ((resolution_status = ANY (ARRAY['open'::text, 'in_progress'::text, 'resolved'::text, 'dismissed'::text])))
);


--
-- Name: gap_reclassification_v1; Type: VIEW; Schema: route_review; Owner: -
--

CREATE VIEW route_review.gap_reclassification_v1 AS
 WITH gap_related_routes AS (
         SELECT cg_1.gap_id,
            count(DISTINCT rj.route_id) FILTER (WHERE (rj.route_id IS NOT NULL)) AS active_related_count,
            count(DISTINCT rj.route_id) FILTER (WHERE (rj.status = 'relation_fetched'::text)) AS fetched_related_count,
            count(DISTINCT rp.route_id) FILTER (WHERE (rp.route_id IS NOT NULL)) AS prod_related_count,
            bool_or((rj.chosen_osm_relation_id IS NOT NULL)) AS any_has_relation,
            array_agg(DISTINCT COALESCE(rj.status, 'unknown'::text) ORDER BY COALESCE(rj.status, 'unknown'::text)) FILTER (WHERE (rj.route_id IS NOT NULL)) AS related_statuses
           FROM (((route_review.coverage_gaps cg_1
             CROSS JOIN LATERAL unnest(cg_1.related_route_ids) rid(route_id))
             LEFT JOIN route_raw.active_route_jobs rj ON ((rj.route_id = rid.route_id)))
             LEFT JOIN route_prod.routes rp ON ((rp.route_id = rid.route_id)))
          GROUP BY cg_1.gap_id
        )
 SELECT (cg.gap_id)::text AS gap_id,
    cg.sector_key,
    cg.sector_label,
    cg.route_family_hint,
    cg.classification_status AS current_gap_status,
    cg.resolution_status,
        CASE
            WHEN (cg.resolution_status = 'resolved'::text) THEN 'resolved'::text
            WHEN (grr.prod_related_count > 0) THEN 'promotion_backlog'::text
            WHEN ((grr.fetched_related_count > 0) AND (grr.active_related_count > 0)) THEN 'matching_backlog'::text
            WHEN (grr.any_has_relation AND (grr.active_related_count > 0)) THEN 'canonicalization_backlog'::text
            WHEN (grr.active_related_count > 0) THEN 'sectorization_backlog'::text
            WHEN ((cardinality(cg.related_route_ids) > 0) AND (grr.active_related_count = 0)) THEN 'fetch_backlog'::text
            ELSE 'true_extraction_gap'::text
        END AS suggested_gap_class,
        CASE
            WHEN (cg.resolution_status = 'resolved'::text) THEN 'gap already resolved'::text
            WHEN (grr.prod_related_count > 0) THEN format('%s related routes in prod, gap may be stale'::text, grr.prod_related_count)
            WHEN (grr.fetched_related_count > 0) THEN format('%s related routes fetched but not promoted'::text, grr.fetched_related_count)
            WHEN grr.any_has_relation THEN 'related routes have chosen relations, need canonicalization'::text
            WHEN (grr.active_related_count > 0) THEN format('%s active related routes exist, need sector/family assignment'::text, grr.active_related_count)
            WHEN ((cardinality(cg.related_route_ids) > 0) AND (grr.active_related_count = 0)) THEN 'related route_ids exist but routes are trashed/missing'::text
            ELSE 'no related routes found, true extraction needed'::text
        END AS suggested_gap_reason,
    COALESCE(grr.active_related_count, (0)::bigint) AS supporting_route_count,
    COALESCE(grr.prod_related_count, (0)::bigint) AS prod_related_count,
    COALESCE(grr.fetched_related_count, (0)::bigint) AS fetched_related_count,
    grr.related_statuses AS strongest_related_evidence,
        CASE
            WHEN (cg.resolution_status = 'resolved'::text) THEN 'none'::text
            WHEN (grr.prod_related_count > 0) THEN 'review_and_resolve_gap'::text
            WHEN (grr.fetched_related_count > 0) THEN 'advance_related_routes_through_pipeline'::text
            WHEN grr.any_has_relation THEN 'canonicalize_related_routes'::text
            WHEN (grr.active_related_count > 0) THEN 'assign_sector_and_fetch'::text
            WHEN (cardinality(cg.related_route_ids) > 0) THEN 'investigate_trashed_related_routes'::text
            ELSE 'extract_from_overpass'::text
        END AS operator_action_hint,
    cg.evidence_summary,
    (cg.related_route_ids)::text[] AS related_route_ids,
    cardinality(cg.related_route_ids) AS related_route_count,
    cg.classification_confidence,
    cg.notes
   FROM (route_review.coverage_gaps cg
     LEFT JOIN gap_related_routes grr ON ((grr.gap_id = cg.gap_id)))
  ORDER BY cg.sector_label, cg.route_family_hint;


--
-- Name: phase3_coverage_gap_catalog_v1; Type: VIEW; Schema: route_review; Owner: -
--

CREATE VIEW route_review.phase3_coverage_gap_catalog_v1 AS
 SELECT (gap_id)::text AS gap_id,
    dedupe_key,
    source_catalog,
    sector_key,
    sector_label,
    route_family_hint,
    known_aliases,
    start_hint,
    end_hint,
    direction_hint,
    evidence_summary,
    (related_route_ids)::text[] AS related_route_ids,
    cardinality(related_route_ids) AS related_route_count,
    classification_status,
    COALESCE(operator_override_classification, classification_status) AS effective_classification,
    classification_confidence,
    classification_source,
    operator_override_classification,
    manual_priority,
    recommended_next_action,
    heuristic_notes,
    resolution_status,
    (resolved_route_id)::text AS resolved_route_id,
    (resolved_prod_route_id)::text AS resolved_prod_route_id,
    reviewed_at,
    reviewed_by,
    notes,
    created_at,
    updated_at
   FROM route_review.coverage_gaps cg
  ORDER BY sector_label, manual_priority DESC, route_family_hint;


--
-- Name: route_job_dedupe_groups; Type: TABLE; Schema: route_review; Owner: -
--

CREATE TABLE route_review.route_job_dedupe_groups (
    dedupe_group_id uuid DEFAULT gen_random_uuid() NOT NULL,
    canonical_route_id uuid NOT NULL,
    chosen_osm_relation_id bigint,
    dedupe_source text DEFAULT 'extractor_relation_id'::text NOT NULL,
    dedupe_reason text DEFAULT 'same_chosen_osm_relation_id'::text NOT NULL,
    evidence_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    group_status text DEFAULT 'proposed'::text NOT NULL,
    reviewable boolean DEFAULT true NOT NULL,
    created_by_system boolean DEFAULT true NOT NULL,
    reviewed_at timestamp with time zone,
    reviewed_by text,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT route_job_dedupe_groups_group_status_check CHECK ((group_status = ANY (ARRAY['proposed'::text, 'confirmed'::text, 'rejected'::text, 'archived'::text])))
);


--
-- Name: route_job_dedupe_memberships; Type: TABLE; Schema: route_review; Owner: -
--

CREATE TABLE route_review.route_job_dedupe_memberships (
    route_id uuid NOT NULL,
    dedupe_group_id uuid NOT NULL,
    canonical_route_id uuid NOT NULL,
    membership_role text DEFAULT 'canonical'::text NOT NULL,
    membership_status text DEFAULT 'active'::text NOT NULL,
    review_status text DEFAULT 'proposed'::text NOT NULL,
    dedupe_reason text DEFAULT 'same_chosen_osm_relation_id'::text NOT NULL,
    reason_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_by_system boolean DEFAULT true NOT NULL,
    reviewed_at timestamp with time zone,
    reviewed_by text,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT route_job_dedupe_memberships_membership_role_check CHECK ((membership_role = ANY (ARRAY['canonical'::text, 'duplicate'::text]))),
    CONSTRAINT route_job_dedupe_memberships_membership_status_check CHECK ((membership_status = ANY (ARRAY['active'::text, 'suppressed'::text, 'restored'::text]))),
    CONSTRAINT route_job_dedupe_memberships_review_status_check CHECK ((review_status = ANY (ARRAY['proposed'::text, 'confirmed'::text, 'rejected'::text, 'restored'::text])))
);


--
-- Name: route_job_canonicalization_v1; Type: VIEW; Schema: route_review; Owner: -
--

CREATE VIEW route_review.route_job_canonicalization_v1 AS
 SELECT (rj.route_id)::text AS route_id,
    (COALESCE(m.canonical_route_id, rj.route_id))::text AS canonical_route_id,
    (m.dedupe_group_id)::text AS dedupe_group_id,
    COALESCE(m.membership_role, 'canonical'::text) AS membership_role,
    COALESCE(m.membership_status, 'active'::text) AS membership_status,
    COALESCE(m.review_status, 'confirmed'::text) AS review_status,
    COALESCE(m.dedupe_reason, 'none'::text) AS dedupe_reason,
    COALESCE(g.group_status, 'confirmed'::text) AS group_status,
    COALESCE(g.reviewable, false) AS reviewable,
    g.chosen_osm_relation_id,
    g.evidence_json
   FROM ((route_raw.active_route_jobs rj
     LEFT JOIN route_review.route_job_dedupe_memberships m ON ((m.route_id = rj.route_id)))
     LEFT JOIN route_review.route_job_dedupe_groups g ON ((g.dedupe_group_id = m.dedupe_group_id)));


--
-- Name: geometry_candidate_sets; Type: TABLE; Schema: route_work; Owner: -
--

CREATE TABLE route_work.geometry_candidate_sets (
    set_id uuid DEFAULT gen_random_uuid() NOT NULL,
    route_id uuid NOT NULL,
    stop_sequence_set_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text,
    generator_version text DEFAULT 'v1'::text NOT NULL,
    notes text
);


--
-- Name: geometry_candidates; Type: TABLE; Schema: route_work; Owner: -
--

CREATE TABLE route_work.geometry_candidates (
    geometry_candidate_id uuid DEFAULT gen_random_uuid() NOT NULL,
    set_id uuid NOT NULL,
    stop_sequence_candidate_id uuid,
    engine text DEFAULT 'valhalla_route'::text NOT NULL,
    preset_id uuid,
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    geom public.geometry(LineString,4326) NOT NULL,
    length_m double precision,
    avg_stop_dist_m double precision,
    max_stop_dist_m double precision,
    score double precision DEFAULT 0 NOT NULL,
    metrics jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    valhalla_request jsonb,
    valhalla_response_hash text
);


--
-- Name: COLUMN geometry_candidates.valhalla_request; Type: COMMENT; Schema: route_work; Owner: -
--

COMMENT ON COLUMN route_work.geometry_candidates.valhalla_request IS 'Full Valhalla request JSON + endpoint_url + valhalla_version + requested_at + request_hash. Captured at trace time so the shape can be re-produced deterministically.';


--
-- Name: COLUMN geometry_candidates.valhalla_response_hash; Type: COMMENT; Schema: route_work; Owner: -
--

COMMENT ON COLUMN route_work.geometry_candidates.valhalla_response_hash IS 'SHA-256 of the raw Valhalla response body, for response-integrity / drift detection across retraces.';


--
-- Name: manual_sequence_exports; Type: TABLE; Schema: route_work; Owner: -
--

CREATE TABLE route_work.manual_sequence_exports (
    export_id uuid DEFAULT gen_random_uuid() NOT NULL,
    route_id uuid NOT NULL,
    service_route_id uuid,
    direction_id smallint,
    source text DEFAULT 'manual_builder'::text NOT NULL,
    ordered_stop_ids uuid[] NOT NULL,
    ordered_node_ids uuid[] DEFAULT ARRAY[]::uuid[] NOT NULL,
    ordered_coords jsonb DEFAULT '[]'::jsonb NOT NULL,
    is_loop boolean DEFAULT false NOT NULL,
    name_hint text,
    operator_hint text,
    variant_hint text,
    created_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    stop_sequence_set_id uuid,
    stop_sequence_candidate_id uuid,
    draft_id uuid,
    coverage_gap_id uuid,
    CONSTRAINT manual_sequence_exports_direction_id_check CHECK ((direction_id = ANY (ARRAY[0, 1])))
);


--
-- Name: relation_stop_prior; Type: TABLE; Schema: route_work; Owner: -
--

CREATE TABLE route_work.relation_stop_prior (
    route_id uuid NOT NULL,
    seq integer NOT NULL,
    member_type text,
    osm_ref bigint,
    osm_node_id bigint,
    role text,
    lat double precision NOT NULL,
    lon double precision NOT NULL,
    matched_stop_node_id uuid,
    match_dist_m double precision,
    match_state text
);


--
-- Name: route_approvals; Type: TABLE; Schema: route_work; Owner: -
--

CREATE TABLE route_work.route_approvals (
    approval_id uuid DEFAULT gen_random_uuid() NOT NULL,
    route_id uuid NOT NULL,
    chosen_geometry_candidate_id uuid NOT NULL,
    chosen_stop_sequence_candidate_id uuid,
    approved_at timestamp with time zone DEFAULT now() NOT NULL,
    approved_by text,
    notes text
);


--
-- Name: stop_sequence_candidate_sets; Type: TABLE; Schema: route_work; Owner: -
--

CREATE TABLE route_work.stop_sequence_candidate_sets (
    set_id uuid DEFAULT gen_random_uuid() NOT NULL,
    route_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text,
    generator_version text DEFAULT 'v1'::text NOT NULL,
    notes text
);


--
-- Name: stop_sequence_candidates; Type: TABLE; Schema: route_work; Owner: -
--

CREATE TABLE route_work.stop_sequence_candidates (
    candidate_id uuid DEFAULT gen_random_uuid() NOT NULL,
    set_id uuid NOT NULL,
    rank integer NOT NULL,
    stop_node_ids uuid[] DEFAULT ARRAY[]::uuid[],
    matched_stops integer,
    avg_match_dist_m double precision,
    max_match_dist_m double precision,
    metrics jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    stop_prior_seqs integer[],
    canonical_coverage double precision
);


--
-- Name: phase3_global_catalog_v1; Type: VIEW; Schema: route_review; Owner: -
--

CREATE VIEW route_review.phase3_global_catalog_v1 AS
 WITH relation_counts AS (
         SELECT relation_candidates.route_id,
            (count(*))::integer AS relation_candidate_count
           FROM route_raw.relation_candidates
          GROUP BY relation_candidates.route_id
        ), chosen_rel_from_candidates AS (
         SELECT DISTINCT ON (rc.route_id) rc.route_id,
            NULLIF(btrim(rc.name), ''::text) AS chosen_rel_name,
            NULLIF(btrim(rc.ref), ''::text) AS chosen_rel_ref,
            NULLIF(btrim(rc.operator), ''::text) AS chosen_rel_operator,
            rc.osm_relation_id AS chosen_rel_osm_id
           FROM (route_raw.relation_candidates rc
             JOIN route_raw.active_route_jobs rj ON (((rj.route_id = rc.route_id) AND (rj.chosen_osm_relation_id = rc.osm_relation_id))))
          ORDER BY rc.route_id, rc.found_at DESC
        ), chosen_rel_from_raw AS (
         SELECT orr.route_id,
            NULLIF(btrim(((rel_elem.value -> 'tags'::text) ->> 'name'::text)), ''::text) AS chosen_rel_name,
            NULLIF(btrim(((rel_elem.value -> 'tags'::text) ->> 'ref'::text)), ''::text) AS chosen_rel_ref,
            NULLIF(btrim(((rel_elem.value -> 'tags'::text) ->> 'operator'::text)), ''::text) AS chosen_rel_operator,
            orr.osm_relation_id AS chosen_rel_osm_id
           FROM ((route_raw.osm_relations_raw orr
             JOIN route_raw.active_route_jobs rj ON (((rj.route_id = orr.route_id) AND (rj.chosen_osm_relation_id = orr.osm_relation_id))))
             CROSS JOIN LATERAL ( SELECT elem.value
                   FROM jsonb_array_elements((orr.overpass_json -> 'elements'::text)) elem(value)
                  WHERE ((elem.value ->> 'type'::text) = 'relation'::text)
                 LIMIT 1) rel_elem)
          WHERE (NOT (EXISTS ( SELECT 1
                   FROM route_raw.relation_candidates rc
                  WHERE ((rc.route_id = orr.route_id) AND (rc.osm_relation_id = orr.osm_relation_id)))))
        ), chosen_relation_info AS (
         SELECT chosen_rel_from_candidates.route_id,
            chosen_rel_from_candidates.chosen_rel_name,
            chosen_rel_from_candidates.chosen_rel_ref,
            chosen_rel_from_candidates.chosen_rel_operator,
            chosen_rel_from_candidates.chosen_rel_osm_id
           FROM chosen_rel_from_candidates
        UNION ALL
         SELECT chosen_rel_from_raw.route_id,
            chosen_rel_from_raw.chosen_rel_name,
            chosen_rel_from_raw.chosen_rel_ref,
            chosen_rel_from_raw.chosen_rel_operator,
            chosen_rel_from_raw.chosen_rel_osm_id
           FROM chosen_rel_from_raw
        ), osm_from_to AS (
         SELECT orr.route_id,
            NULLIF(btrim(((rel_elem.value -> 'tags'::text) ->> 'from'::text)), ''::text) AS osm_from,
            NULLIF(btrim(((rel_elem.value -> 'tags'::text) ->> 'to'::text)), ''::text) AS osm_to,
            NULLIF(btrim(((rel_elem.value -> 'tags'::text) ->> 'network'::text)), ''::text) AS osm_network,
                CASE
                    WHEN ((NULLIF(btrim(((rel_elem.value -> 'tags'::text) ->> 'from'::text)), ''::text) IS NOT NULL) AND (NULLIF(btrim(((rel_elem.value -> 'tags'::text) ->> 'to'::text)), ''::text) IS NOT NULL)) THEN ((btrim(((rel_elem.value -> 'tags'::text) ->> 'from'::text)) || ' – '::text) || btrim(((rel_elem.value -> 'tags'::text) ->> 'to'::text)))
                    ELSE NULL::text
                END AS osm_from_to_label
           FROM ((route_raw.osm_relations_raw orr
             JOIN route_raw.active_route_jobs rj ON ((rj.route_id = orr.route_id)))
             CROSS JOIN LATERAL ( SELECT elem.value
                   FROM jsonb_array_elements((orr.overpass_json -> 'elements'::text)) elem(value)
                  WHERE ((elem.value ->> 'type'::text) = 'relation'::text)
                 LIMIT 1) rel_elem)
        ), prior_stats AS (
         SELECT relation_stop_prior.route_id,
            (count(*))::integer AS prior_stop_count,
            (sum(
                CASE
                    WHEN (relation_stop_prior.matched_stop_node_id IS NOT NULL) THEN 1
                    ELSE 0
                END))::integer AS matched_count,
            (sum(
                CASE
                    WHEN (COALESCE(relation_stop_prior.match_state, ''::text) = 'ambiguous'::text) THEN 1
                    ELSE 0
                END))::integer AS ambiguous_count,
            (sum(
                CASE
                    WHEN ((relation_stop_prior.matched_stop_node_id IS NULL) AND (COALESCE(relation_stop_prior.match_state, ''::text) <> 'ambiguous'::text)) THEN 1
                    ELSE 0
                END))::integer AS unmatched_count
           FROM route_work.relation_stop_prior
          GROUP BY relation_stop_prior.route_id
        ), sequence_counts AS (
         SELECT scs.route_id,
            (count(*))::integer AS stop_sequence_set_count,
            (COALESCE(sum(COALESCE(scc.n_candidates, 0)), (0)::bigint))::integer AS stop_sequence_candidate_count
           FROM (route_work.stop_sequence_candidate_sets scs
             LEFT JOIN ( SELECT stop_sequence_candidates.set_id,
                    (count(*))::integer AS n_candidates
                   FROM route_work.stop_sequence_candidates
                  GROUP BY stop_sequence_candidates.set_id) scc ON ((scc.set_id = scs.set_id)))
          GROUP BY scs.route_id
        ), geometry_counts AS (
         SELECT gcs.route_id,
            (count(*))::integer AS geometry_set_count,
            (COALESCE(sum(COALESCE(gcc.n_candidates, 0)), (0)::bigint))::integer AS geometry_candidate_count
           FROM (route_work.geometry_candidate_sets gcs
             LEFT JOIN ( SELECT geometry_candidates.set_id,
                    (count(*))::integer AS n_candidates
                   FROM route_work.geometry_candidates
                  GROUP BY geometry_candidates.set_id) gcc ON ((gcc.set_id = gcs.set_id)))
          GROUP BY gcs.route_id
        ), route_approval AS (
         SELECT route_approvals.route_id,
            max(route_approvals.approved_at) AS route_approved_at
           FROM route_work.route_approvals
          GROUP BY route_approvals.route_id
        ), manual_stats AS (
         SELECT manual_sequence_exports.route_id,
            (count(*))::integer AS manual_export_count,
            bool_or((COALESCE(manual_sequence_exports.source, ''::text) = 'manual_builder'::text)) AS manual_origin,
            max(manual_sequence_exports.created_at) AS latest_manual_export_at,
            (array_agg((manual_sequence_exports.export_id)::text ORDER BY manual_sequence_exports.created_at DESC))[1] AS latest_manual_export_id,
            (array_agg((manual_sequence_exports.coverage_gap_id)::text ORDER BY manual_sequence_exports.created_at DESC))[1] AS latest_coverage_gap_id
           FROM route_work.manual_sequence_exports
          GROUP BY manual_sequence_exports.route_id
        ), dedupe AS (
         SELECT (route_job_canonicalization_v1.route_id)::uuid AS route_id,
            (route_job_canonicalization_v1.canonical_route_id)::uuid AS canonical_route_id,
            (route_job_canonicalization_v1.dedupe_group_id)::uuid AS dedupe_group_id,
            route_job_canonicalization_v1.membership_role,
            route_job_canonicalization_v1.membership_status,
            route_job_canonicalization_v1.review_status,
            route_job_canonicalization_v1.dedupe_reason,
            route_job_canonicalization_v1.group_status,
            route_job_canonicalization_v1.reviewable
           FROM route_review.route_job_canonicalization_v1
        ), catalog AS (
         SELECT rj.route_id,
            COALESCE(d.canonical_route_id, rj.route_id) AS canonical_route_id,
            d.dedupe_group_id,
            COALESCE(d.membership_role, 'canonical'::text) AS dedupe_membership_role,
            COALESCE(d.membership_status, 'active'::text) AS dedupe_membership_status,
            COALESCE(d.review_status, 'confirmed'::text) AS dedupe_review_status,
            COALESCE(d.group_status, 'confirmed'::text) AS dedupe_group_status,
            COALESCE(d.reviewable, false) AS dedupe_reviewable,
            COALESCE(d.dedupe_reason, 'none'::text) AS dedupe_reason,
            (rj.route_id)::text AS route_job_id,
            rj.created_at AS route_job_created_at,
            rj.created_by,
            COALESCE(rj.status, 'new'::text) AS route_job_status,
            rj.notes,
            rj.area_key,
            rj.bbox,
            rj.known_ref,
            rj.chosen_osm_relation_id,
            rj.extractor_source,
            rj.extractor_review,
            COALESCE(rc.relation_candidate_count, 0) AS relation_candidate_count,
            (COALESCE(sr.service_route_id, rj.service_route_id))::text AS service_route_id,
            (COALESCE(rj.direction_id, srd.direction_id))::integer AS direction_id,
            COALESCE(sr.route_ref, NULLIF(rj.known_ref, ''::text)) AS service_route_ref,
            sr.route_name AS service_route_name,
            sr.operator_name AS service_route_operator,
            COALESCE(sr.route_approval_status, 'pending'::text) AS service_route_status,
            COALESCE(srd.phase3_progress_step, 0) AS phase3_progress_step,
            COALESCE(srd.direction_approval_status, 'pending'::text) AS direction_approval_status,
            COALESCE(srd.geom_source, 'unknown'::text) AS geom_source,
            (rp.route_id IS NOT NULL) AS has_prod_route,
            rp.route_name AS prod_route_name,
            rp.route_aliases,
            rp.human_verified,
            ps.prior_stop_count,
            ps.matched_count,
            ps.unmatched_count,
            ps.ambiguous_count,
            COALESCE(sc.stop_sequence_set_count, 0) AS stop_sequence_set_count,
            COALESCE(sc.stop_sequence_candidate_count, 0) AS stop_sequence_candidate_count,
            COALESCE(gc.geometry_set_count, 0) AS geometry_set_count,
            COALESCE(gc.geometry_candidate_count, 0) AS geometry_candidate_count,
            (ra.route_id IS NOT NULL) AS has_route_approval,
            ms.manual_export_count,
            COALESCE(ms.manual_origin, false) AS manual_origin,
            ms.latest_manual_export_at,
            ms.latest_manual_export_id,
            ms.latest_coverage_gap_id,
            NULLIF(btrim((rj.extractor_review #>> '{target,group}'::text[])), ''::text) AS target_group,
            NULLIF(btrim((rj.extractor_review #>> '{target,place_bundle}'::text[])), ''::text) AS target_place_bundle,
            NULLIF(btrim((rj.extractor_review #>> '{target,place}'::text[])), ''::text) AS target_place,
            NULLIF(btrim((rj.extractor_review #>> '{geography,sector_hint}'::text[])), ''::text) AS sector_hint,
            NULLIF(btrim((rj.extractor_review #>> '{geography,corridor_hint}'::text[])), ''::text) AS corridor_hint,
            NULLIF(btrim((rj.extractor_review #>> '{hints,route_hint_raw}'::text[])), ''::text) AS route_hint,
            NULLIF(btrim((rj.extractor_review #>> '{hints,cooperative_hint}'::text[])), ''::text) AS cooperative_hint,
            NULLIF(btrim((rj.extractor_review #>> '{source_document}'::text[])), ''::text) AS source_document,
            NULLIF(btrim((rj.extractor_review #>> '{dedupe,novelty_status}'::text[])), ''::text) AS extractor_novelty_status,
            NULLIF(btrim((rj.extractor_review #>> '{fetch,fetch_status}'::text[])), ''::text) AS fetch_status,
            NULLIF(btrim((rj.extractor_review #>> '{discover,signal_strength}'::text[])), ''::text) AS extractor_signal_strength,
            cri.chosen_rel_name,
            cri.chosen_rel_ref,
            cri.chosen_rel_operator,
            oft.osm_from,
            oft.osm_to,
            oft.osm_from_to_label,
            oft.osm_network
           FROM (((((((((((((route_raw.active_route_jobs rj
             LEFT JOIN relation_counts rc ON ((rc.route_id = rj.route_id)))
             LEFT JOIN chosen_relation_info cri ON ((cri.route_id = rj.route_id)))
             LEFT JOIN osm_from_to oft ON ((oft.route_id = rj.route_id)))
             LEFT JOIN route_review.route_job_canonicalization_v1 d0 ON (((d0.route_id)::uuid = rj.route_id)))
             LEFT JOIN dedupe d ON ((d.route_id = rj.route_id)))
             LEFT JOIN route_raw.service_route_directions srd ON ((srd.route_id = rj.route_id)))
             LEFT JOIN route_raw.service_routes sr ON ((sr.service_route_id = COALESCE(rj.service_route_id, srd.service_route_id))))
             LEFT JOIN route_prod.routes rp ON ((rp.route_id = rj.route_id)))
             LEFT JOIN prior_stats ps ON ((ps.route_id = rj.route_id)))
             LEFT JOIN sequence_counts sc ON ((sc.route_id = rj.route_id)))
             LEFT JOIN geometry_counts gc ON ((gc.route_id = rj.route_id)))
             LEFT JOIN route_approval ra ON ((ra.route_id = rj.route_id)))
             LEFT JOIN manual_stats ms ON ((ms.route_id = rj.route_id)))
        )
 SELECT route_job_id,
    (canonical_route_id)::text AS canonical_route_job_id,
    (dedupe_group_id)::text AS dedupe_group_id,
    dedupe_membership_role,
    dedupe_membership_status,
    dedupe_review_status,
    dedupe_group_status,
    dedupe_reviewable,
    dedupe_reason,
    (route_job_id = (canonical_route_id)::text) AS is_canonical_route_job,
    (dedupe_membership_status = 'suppressed'::text) AS is_suppressed_duplicate,
    service_route_id,
    direction_id,
    service_route_ref,
    service_route_name,
    prod_route_name,
    service_route_operator,
    COALESCE(NULLIF(btrim(service_route_name), ''::text), NULLIF(btrim(prod_route_name), ''::text), NULLIF(btrim(service_route_ref), ''::text), NULLIF(btrim(chosen_rel_name), ''::text), NULLIF(btrim(chosen_rel_ref), ''::text), NULLIF(btrim(osm_from_to_label), ''::text), NULLIF(btrim(route_hint), ''::text), NULLIF(btrim(target_place), ''::text), ('route_'::text || "left"(route_job_id, 8))) AS route_family_label,
        CASE
            WHEN (dedupe_group_id IS NOT NULL) THEN (canonical_route_id)::text
            ELSE COALESCE(service_route_id, (canonical_route_id)::text, route_job_id)
        END AS route_family_key,
    route_job_created_at,
    created_by,
    route_job_status,
    notes,
    chosen_osm_relation_id,
    extractor_source,
    area_key,
    bbox,
    known_ref,
    target_group,
    target_place_bundle,
    target_place,
    sector_hint,
    corridor_hint,
    route_hint,
    cooperative_hint,
    source_document,
    extractor_novelty_status,
    fetch_status,
    extractor_signal_strength,
    COALESCE(relation_candidate_count, 0) AS relation_candidate_count,
    COALESCE(prior_stop_count, 0) AS prior_stop_count,
    COALESCE(matched_count, 0) AS matched_count,
    COALESCE(unmatched_count, 0) AS unmatched_count,
    COALESCE(ambiguous_count, 0) AS ambiguous_count,
    COALESCE(stop_sequence_set_count, 0) AS stop_sequence_set_count,
    COALESCE(stop_sequence_candidate_count, 0) AS stop_sequence_candidate_count,
    COALESCE(geometry_set_count, 0) AS geometry_set_count,
    COALESCE(geometry_candidate_count, 0) AS geometry_candidate_count,
    COALESCE(phase3_progress_step, 0) AS phase3_progress_step,
    direction_approval_status,
    geom_source,
    service_route_status,
        CASE
            WHEN (chosen_osm_relation_id IS NOT NULL) THEN 'complete'::text
            WHEN (COALESCE(relation_candidate_count, 0) > 0) THEN 'candidates_found'::text
            ELSE 'not_started'::text
        END AS step05_state,
        CASE
            WHEN (COALESCE(prior_stop_count, 0) <= 0) THEN 'not_started'::text
            WHEN (COALESCE(ambiguous_count, 0) > 0) THEN 'blocked_ambiguous'::text
            WHEN (COALESCE(unmatched_count, 0) > 0) THEN 'blocked_unmatched'::text
            WHEN (COALESCE(matched_count, 0) >= COALESCE(prior_stop_count, 0)) THEN 'complete'::text
            ELSE 'partial'::text
        END AS step20_state,
        CASE
            WHEN has_prod_route THEN 'prod'::text
            WHEN has_route_approval THEN 'approved'::text
            WHEN (COALESCE(geometry_candidate_count, 0) > 0) THEN 'generated'::text
            WHEN (COALESCE(stop_sequence_candidate_count, 0) > 0) THEN 'sequence_ready'::text
            WHEN (chosen_osm_relation_id IS NOT NULL) THEN 'extracted'::text
            ELSE 'not_started'::text
        END AS geometry_status,
        CASE
            WHEN has_prod_route THEN 'prod'::text
            WHEN has_route_approval THEN 'approved'::text
            WHEN (COALESCE(direction_approval_status, 'pending'::text) = 'ready'::text) THEN 'ready'::text
            ELSE 'pending'::text
        END AS approval_status,
        CASE
            WHEN has_prod_route THEN 'in_prod'::text
            ELSE 'not_in_prod'::text
        END AS prod_status,
    (COALESCE(manual_origin, false) OR (COALESCE(geom_source, 'unknown'::text) = 'manual'::text)) AS manual_origin,
    COALESCE(manual_export_count, 0) AS manual_export_count,
    latest_manual_export_at,
    latest_manual_export_id,
    latest_coverage_gap_id,
    COALESCE(NULLIF(btrim(sector_hint), ''::text), NULLIF(btrim(replace(target_group, '_'::text, ' '::text)), ''::text), NULLIF(btrim(replace(target_place_bundle, '_'::text, ' '::text)), ''::text), NULLIF(btrim(replace(area_key, '_'::text, ' '::text)), ''::text), 'Unassigned'::text) AS sector_label,
    COALESCE(NULLIF(btrim(sector_hint), ''::text), NULLIF(btrim(target_group), ''::text), NULLIF(btrim(target_place_bundle), ''::text), NULLIF(btrim(area_key), ''::text), 'unassigned'::text) AS sector_key,
    jsonb_build_object('extractor_novelty_status', extractor_novelty_status, 'fetch_status', fetch_status, 'prior_stop_count', COALESCE(prior_stop_count, 0), 'unmatched_count', COALESCE(unmatched_count, 0), 'ambiguous_count', COALESCE(ambiguous_count, 0), 'geometry_candidate_count', COALESCE(geometry_candidate_count, 0)) AS diagnostics_summary,
    chosen_rel_name,
    chosen_rel_ref,
    chosen_rel_operator,
    osm_from,
    osm_to,
    osm_from_to_label,
    osm_network,
    COALESCE(NULLIF(btrim(chosen_rel_operator), ''::text), NULLIF(btrim(service_route_operator), ''::text), NULLIF(btrim(cooperative_hint), ''::text)) AS operator_hint,
    NULLIF(btrim(cooperative_hint), ''::text) AS operator_hint_raw,
        CASE
            WHEN ((NULLIF(btrim(chosen_rel_operator), ''::text) IS NOT NULL) AND (((NULLIF(btrim(cooperative_hint), ''::text) IS NOT NULL) AND (upper(btrim(chosen_rel_operator)) <> upper(btrim(cooperative_hint)))) OR ((NULLIF(btrim(service_route_operator), ''::text) IS NOT NULL) AND (upper(btrim(chosen_rel_operator)) <> upper(btrim(service_route_operator)))))) THEN true
            ELSE false
        END AS operator_conflict_flag,
        CASE
            WHEN (NULLIF(btrim(chosen_rel_operator), ''::text) IS NOT NULL) THEN 'chosen_rel_operator'::text
            WHEN (NULLIF(btrim(service_route_operator), ''::text) IS NOT NULL) THEN 'service_route_operator'::text
            WHEN (NULLIF(btrim(cooperative_hint), ''::text) IS NOT NULL) THEN 'cooperative_hint'::text
            ELSE NULL::text
        END AS operator_source_used,
        CASE
            WHEN ((lower(COALESCE(notes, ''::text)) ~~ '%test%'::text) OR (lower(COALESCE(notes, ''::text)) ~~ '%trash%'::text) OR (lower(COALESCE(known_ref, ''::text)) ~~ '%test%'::text) OR (lower(COALESCE(route_job_status, ''::text)) ~~ '%test%'::text)) THEN 'test_quarantine'::text
            WHEN (route_job_status = 'merged_duplicate'::text) THEN 'merged_duplicate'::text
            WHEN (route_job_status = 'extraction_failed'::text) THEN 'extraction_failed'::text
            ELSE 'active'::text
        END AS inventory_status
   FROM catalog c;


--
-- Name: phase3_sector_coverage_v1; Type: VIEW; Schema: route_review; Owner: -
--

CREATE VIEW route_review.phase3_sector_coverage_v1 AS
 WITH family_rollup AS (
         SELECT phase3_global_catalog_v1.sector_key,
            COALESCE(NULLIF(btrim(replace(phase3_global_catalog_v1.sector_label, '_'::text, ' '::text)), ''::text), NULLIF(btrim(replace(phase3_global_catalog_v1.sector_key, '_'::text, ' '::text)), ''::text), 'unassigned'::text) AS sector_label,
            phase3_global_catalog_v1.route_family_key,
            max(phase3_global_catalog_v1.route_family_label) AS route_family_label,
            bool_or((phase3_global_catalog_v1.step05_state = 'complete'::text)) AS any_extracted,
            bool_or((phase3_global_catalog_v1.step20_state = 'complete'::text)) AS any_step20_complete,
            bool_or((phase3_global_catalog_v1.geometry_status = ANY (ARRAY['generated'::text, 'approved'::text, 'prod'::text]))) AS any_geometry,
            bool_or((phase3_global_catalog_v1.approval_status = ANY (ARRAY['approved'::text, 'prod'::text]))) AS any_approved,
            bool_or((phase3_global_catalog_v1.prod_status = 'in_prod'::text)) AS any_prod,
            bool_or((phase3_global_catalog_v1.dedupe_membership_status = 'suppressed'::text)) AS any_suppressed,
            bool_or(phase3_global_catalog_v1.manual_origin) AS any_manual,
            bool_or(((phase3_global_catalog_v1.direction_id = 0) AND (phase3_global_catalog_v1.direction_approval_status = ANY (ARRAY['ready'::text, 'approved'::text])))) AS direction0_ready,
            bool_or(((phase3_global_catalog_v1.direction_id = 1) AND (phase3_global_catalog_v1.direction_approval_status = ANY (ARRAY['ready'::text, 'approved'::text])))) AS direction1_ready
           FROM route_review.phase3_global_catalog_v1
          WHERE (phase3_global_catalog_v1.inventory_status = 'active'::text)
          GROUP BY phase3_global_catalog_v1.sector_key, phase3_global_catalog_v1.sector_label, phase3_global_catalog_v1.route_family_key
        )
 SELECT sector_key,
    max(sector_label) AS sector_label,
    (count(*))::integer AS route_family_count,
    (sum(
        CASE
            WHEN any_extracted THEN 1
            ELSE 0
        END))::integer AS extracted_family_count,
    (sum(
        CASE
            WHEN any_step20_complete THEN 1
            ELSE 0
        END))::integer AS step20_complete_family_count,
    (sum(
        CASE
            WHEN any_geometry THEN 1
            ELSE 0
        END))::integer AS geometry_ready_family_count,
    (sum(
        CASE
            WHEN any_approved THEN 1
            ELSE 0
        END))::integer AS approved_family_count,
    (sum(
        CASE
            WHEN any_prod THEN 1
            ELSE 0
        END))::integer AS prod_family_count,
    (sum(
        CASE
            WHEN any_manual THEN 1
            ELSE 0
        END))::integer AS manual_family_count,
    (sum(
        CASE
            WHEN any_suppressed THEN 1
            ELSE 0
        END))::integer AS suppressed_family_count,
    (sum(
        CASE
            WHEN (any_extracted AND (NOT any_prod)) THEN 1
            ELSE 0
        END))::integer AS extracted_not_prod_count,
    (sum(
        CASE
            WHEN direction0_ready THEN 1
            ELSE 0
        END))::integer AS direction0_ready_count,
    (sum(
        CASE
            WHEN direction1_ready THEN 1
            ELSE 0
        END))::integer AS direction1_ready_count,
    (sum(
        CASE
            WHEN ((NOT any_prod) OR (NOT (direction0_ready AND direction1_ready))) THEN 1
            ELSE 0
        END))::integer AS incomplete_family_count,
    array_remove(array_agg(DISTINCT route_family_label ORDER BY route_family_label), NULL::text) AS route_families_present
   FROM family_rollup
  GROUP BY sector_key
  ORDER BY (max(sector_label)), sector_key;


--
-- Name: route_interpretation_v1; Type: VIEW; Schema: route_review; Owner: -
--

CREATE VIEW route_review.route_interpretation_v1 AS
 WITH osm_rel_tags AS (
         SELECT DISTINCT ON (orr.route_id) orr.route_id,
            orr.osm_relation_id,
            NULLIF(btrim(((rel_elem.value -> 'tags'::text) ->> 'name'::text)), ''::text) AS osm_name,
            NULLIF(btrim(((rel_elem.value -> 'tags'::text) ->> 'ref'::text)), ''::text) AS osm_ref,
            NULLIF(btrim(((rel_elem.value -> 'tags'::text) ->> 'operator'::text)), ''::text) AS osm_operator,
            NULLIF(btrim(((rel_elem.value -> 'tags'::text) ->> 'from'::text)), ''::text) AS osm_from,
            NULLIF(btrim(((rel_elem.value -> 'tags'::text) ->> 'to'::text)), ''::text) AS osm_to,
            NULLIF(btrim(((rel_elem.value -> 'tags'::text) ->> 'network'::text)), ''::text) AS osm_network
           FROM ((route_raw.osm_relations_raw orr
             JOIN route_raw.active_route_jobs rj ON (((rj.route_id = orr.route_id) AND (rj.chosen_osm_relation_id = orr.osm_relation_id))))
             CROSS JOIN LATERAL ( SELECT elem.value
                   FROM jsonb_array_elements((orr.overpass_json -> 'elements'::text)) elem(value)
                  WHERE ((elem.value ->> 'type'::text) = 'relation'::text)
                 LIMIT 1) rel_elem)
          ORDER BY orr.route_id, orr.fetched_at DESC
        )
 SELECT c.route_job_id,
    c.route_family_label,
    c.sector_key,
    c.sector_label,
    c.inventory_status,
    c.chosen_rel_name,
    c.chosen_rel_ref,
    c.chosen_rel_operator,
    c.operator_hint,
    c.target_place,
    c.target_group,
    c.target_place_bundle,
    c.cooperative_hint,
    c.known_ref,
    c.route_hint,
    c.osm_from,
    c.osm_to,
    c.osm_from_to_label,
    c.osm_network,
    c.route_job_status,
    c.chosen_osm_relation_id,
    COALESCE(NULLIF(btrim(c.chosen_rel_name), ''::text), ort.osm_name, NULLIF(btrim(c.service_route_name), ''::text), NULLIF(btrim(c.prod_route_name), ''::text),
        CASE
            WHEN ((ort.osm_from IS NOT NULL) AND (ort.osm_to IS NOT NULL)) THEN ((ort.osm_from || ' – '::text) || ort.osm_to)
            ELSE NULL::text
        END, NULLIF(btrim(c.route_hint), ''::text), NULLIF(btrim(c.target_place), ''::text)) AS interpreted_name,
    COALESCE(NULLIF(btrim(c.chosen_rel_ref), ''::text), ort.osm_ref, NULLIF(btrim(c.service_route_ref), ''::text), NULLIF(btrim(c.known_ref), ''::text)) AS interpreted_ref,
    COALESCE(NULLIF(btrim(c.chosen_rel_operator), ''::text), ort.osm_operator, NULLIF(btrim(c.service_route_operator), ''::text), NULLIF(btrim(c.cooperative_hint), ''::text)) AS interpreted_operator,
    COALESCE(c.osm_from, ort.osm_from) AS interpreted_from,
    COALESCE(c.osm_to, ort.osm_to) AS interpreted_to,
        CASE
            WHEN (c.route_family_label !~ '^route_[0-9a-f]+'::text) THEN 'canonical'::text
            WHEN (ort.osm_name IS NOT NULL) THEN 'osm_recoverable'::text
            WHEN ((ort.osm_from IS NOT NULL) AND (ort.osm_to IS NOT NULL)) THEN 'from_to_recoverable'::text
            WHEN (ort.osm_ref IS NOT NULL) THEN 'ref_recoverable'::text
            WHEN ((c.route_hint IS NOT NULL) OR (c.target_place IS NOT NULL)) THEN 'hint_recoverable'::text
            ELSE 'unresolved_synthetic'::text
        END AS label_quality,
        CASE
            WHEN (c.route_family_label !~ '^route_[0-9a-f]+'::text) THEN 1.0
            WHEN ((ort.osm_name IS NOT NULL) AND (ort.osm_operator IS NOT NULL)) THEN 0.95
            WHEN (ort.osm_name IS NOT NULL) THEN 0.9
            WHEN ((ort.osm_from IS NOT NULL) AND (ort.osm_to IS NOT NULL)) THEN 0.85
            WHEN (ort.osm_ref IS NOT NULL) THEN 0.8
            WHEN (c.route_hint IS NOT NULL) THEN 0.6
            WHEN (c.target_place IS NOT NULL) THEN 0.4
            ELSE 0.0
        END AS label_confidence,
        CASE
            WHEN (c.route_family_label !~ '^route_[0-9a-f]+'::text) THEN 'catalog_canonical'::text
            WHEN (ort.osm_name IS NOT NULL) THEN 'osm_relation_raw_tags'::text
            WHEN (ort.osm_from IS NOT NULL) THEN 'osm_relation_raw_from_to'::text
            WHEN (ort.osm_ref IS NOT NULL) THEN 'osm_relation_raw_ref'::text
            WHEN (c.route_hint IS NOT NULL) THEN 'extractor_hint'::text
            WHEN (c.target_place IS NOT NULL) THEN 'extractor_target'::text
            ELSE 'none'::text
        END AS label_evidence_source,
        CASE
            WHEN (c.route_family_label !~ '^route_[0-9a-f]+'::text) THEN NULL::text
            WHEN ((ort.osm_name IS NOT NULL) OR (ort.osm_from IS NOT NULL) OR (ort.osm_ref IS NOT NULL)) THEN NULL::text
            WHEN ((c.route_hint IS NOT NULL) OR (c.target_place IS NOT NULL)) THEN NULL::text
            WHEN ((c.chosen_osm_relation_id IS NOT NULL) AND (ort.osm_relation_id IS NULL)) THEN 'relation_raw_not_fetched'::text
            WHEN (c.chosen_osm_relation_id IS NULL) THEN 'no_chosen_relation'::text
            ELSE 'relation_has_no_tags'::text
        END AS unresolved_reason
   FROM (route_review.phase3_global_catalog_v1 c
     LEFT JOIN osm_rel_tags ort ON ((ort.route_id = (c.route_job_id)::uuid)))
  WHERE (c.inventory_status = 'active'::text);


--
-- Name: sector_suggestion_v1; Type: VIEW; Schema: route_review; Owner: -
--

CREATE VIEW route_review.sector_suggestion_v1 AS
 WITH operator_sector_map AS (
         SELECT lower(btrim(phase3_global_catalog_v1.operator_hint)) AS operator_key,
            phase3_global_catalog_v1.sector_key,
            phase3_global_catalog_v1.sector_label,
            count(*) AS assignment_count
           FROM route_review.phase3_global_catalog_v1
          WHERE ((phase3_global_catalog_v1.inventory_status = 'active'::text) AND (phase3_global_catalog_v1.operator_hint IS NOT NULL) AND (phase3_global_catalog_v1.sector_key IS NOT NULL) AND (phase3_global_catalog_v1.sector_key <> 'unassigned'::text) AND (btrim(phase3_global_catalog_v1.sector_key) <> ''::text))
          GROUP BY (lower(btrim(phase3_global_catalog_v1.operator_hint))), phase3_global_catalog_v1.sector_key, phase3_global_catalog_v1.sector_label
        ), operator_best_sector AS (
         SELECT DISTINCT ON (operator_sector_map.operator_key) operator_sector_map.operator_key,
            operator_sector_map.sector_key AS op_suggested_sector,
            operator_sector_map.sector_label AS op_suggested_sector_label,
            operator_sector_map.assignment_count AS op_sector_evidence_count
           FROM operator_sector_map
          ORDER BY operator_sector_map.operator_key, operator_sector_map.assignment_count DESC
        ), interp AS (
         SELECT route_interpretation_v1.route_job_id,
            route_interpretation_v1.route_family_label,
            route_interpretation_v1.sector_key,
            route_interpretation_v1.sector_label,
            route_interpretation_v1.inventory_status,
            route_interpretation_v1.chosen_rel_name,
            route_interpretation_v1.chosen_rel_ref,
            route_interpretation_v1.chosen_rel_operator,
            route_interpretation_v1.operator_hint,
            route_interpretation_v1.target_place,
            route_interpretation_v1.target_group,
            route_interpretation_v1.target_place_bundle,
            route_interpretation_v1.cooperative_hint,
            route_interpretation_v1.known_ref,
            route_interpretation_v1.route_hint,
            route_interpretation_v1.osm_from,
            route_interpretation_v1.osm_to,
            route_interpretation_v1.osm_from_to_label,
            route_interpretation_v1.osm_network,
            route_interpretation_v1.route_job_status,
            route_interpretation_v1.chosen_osm_relation_id,
            route_interpretation_v1.interpreted_name,
            route_interpretation_v1.interpreted_ref,
            route_interpretation_v1.interpreted_operator,
            route_interpretation_v1.interpreted_from,
            route_interpretation_v1.interpreted_to,
            route_interpretation_v1.label_quality,
            route_interpretation_v1.label_confidence,
            route_interpretation_v1.label_evidence_source,
            route_interpretation_v1.unresolved_reason
           FROM route_review.route_interpretation_v1
        ), route_pattern_sector AS (
         SELECT i_1.route_job_id,
                NULL::text AS pattern_sector
           FROM interp i_1
          WHERE ((i_1.sector_key IS NULL) OR (i_1.sector_key = 'unassigned'::text) OR (btrim(i_1.sector_key) = ''::text))
        )
 SELECT i.route_job_id,
    i.route_family_label,
    i.sector_key AS current_sector,
    i.sector_label AS current_sector_label,
    i.interpreted_name,
    i.interpreted_ref,
    i.interpreted_operator,
    i.interpreted_from,
    i.interpreted_to,
    i.label_quality,
    i.label_confidence,
    i.label_evidence_source,
    i.unresolved_reason,
    COALESCE(obs.op_suggested_sector,
        CASE
            WHEN (i.interpreted_operator IS NOT NULL) THEN ( SELECT os2.op_suggested_sector
               FROM operator_best_sector os2
              WHERE (os2.operator_key = lower(btrim(i.interpreted_operator)))
             LIMIT 1)
            ELSE NULL::text
        END, rps.pattern_sector) AS suggested_sector,
    COALESCE(obs.op_suggested_sector_label,
        CASE
            WHEN (i.interpreted_operator IS NOT NULL) THEN ( SELECT os2.op_suggested_sector_label
               FROM operator_best_sector os2
              WHERE (os2.operator_key = lower(btrim(i.interpreted_operator)))
             LIMIT 1)
            ELSE NULL::text
        END, replace(COALESCE(rps.pattern_sector, ''::text), '_'::text, ' '::text)) AS suggested_sector_label,
        CASE
            WHEN (obs.op_suggested_sector IS NOT NULL) THEN 'operator_hint_match'::text
            WHEN ((i.interpreted_operator IS NOT NULL) AND (EXISTS ( SELECT 1
               FROM operator_best_sector os2
              WHERE (os2.operator_key = lower(btrim(i.interpreted_operator)))))) THEN 'interpreted_operator_match'::text
            WHEN (rps.pattern_sector IS NOT NULL) THEN 'route_name_pattern'::text
            WHEN (i.target_place IS NOT NULL) THEN 'target_place_available'::text
            WHEN (i.target_group IS NOT NULL) THEN 'target_group_available'::text
            ELSE 'no_evidence'::text
        END AS suggestion_reason,
        CASE
            WHEN ((obs.op_suggested_sector IS NOT NULL) AND (COALESCE(obs.op_sector_evidence_count, (0)::bigint) >= 3)) THEN 0.9
            WHEN (obs.op_suggested_sector IS NOT NULL) THEN 0.7
            WHEN ((i.interpreted_operator IS NOT NULL) AND (EXISTS ( SELECT 1
               FROM operator_best_sector os2
              WHERE (os2.operator_key = lower(btrim(i.interpreted_operator)))))) THEN 0.75
            WHEN (rps.pattern_sector IS NOT NULL) THEN 0.65
            WHEN (i.target_place IS NOT NULL) THEN 0.5
            ELSE 0.0
        END AS sector_confidence,
    jsonb_build_object('operator_hint', i.operator_hint, 'interpreted_operator', i.interpreted_operator, 'interpreted_from', i.interpreted_from, 'interpreted_to', i.interpreted_to, 'target_place', i.target_place, 'target_group', i.target_group, 'cooperative_hint', i.cooperative_hint, 'known_ref', i.known_ref, 'current_sector', i.sector_key, 'label_quality', i.label_quality, 'pattern_sector', rps.pattern_sector) AS evidence_summary,
    'deterministic'::text AS suggestion_source,
    'pending'::text AS operator_review_status
   FROM ((interp i
     LEFT JOIN operator_best_sector obs ON ((obs.operator_key = lower(btrim(i.operator_hint)))))
     LEFT JOIN route_pattern_sector rps ON ((rps.route_job_id = i.route_job_id)))
  WHERE ((i.sector_key IS NULL) OR (i.sector_key = 'unassigned'::text) OR (btrim(i.sector_key) = ''::text));


--
-- Name: delete_events; Type: TABLE; Schema: route_trash; Owner: -
--

CREATE TABLE route_trash.delete_events (
    delete_event_id uuid DEFAULT gen_random_uuid() NOT NULL,
    route_id uuid,
    original_table text,
    original_primary_key text,
    action_type text NOT NULL,
    workflow_source text NOT NULL,
    reason text,
    actor text,
    event_at timestamp with time zone DEFAULT now() NOT NULL,
    trash_id uuid,
    replacement_route_id uuid,
    canonical_route_id uuid,
    metadata_jsonb jsonb DEFAULT '{}'::jsonb,
    CONSTRAINT delete_events_action_type_check CHECK ((action_type = ANY (ARRAY['delete'::text, 'deactivate'::text, 'merge'::text, 'replace'::text, 'cleanup'::text, 'purge'::text, 'restore'::text, 'suppress'::text])))
);


--
-- Name: trash_items; Type: TABLE; Schema: route_trash; Owner: -
--

CREATE TABLE route_trash.trash_items (
    trash_id uuid DEFAULT gen_random_uuid() NOT NULL,
    original_table text NOT NULL,
    original_primary_key text NOT NULL,
    route_id uuid NOT NULL,
    chosen_osm_relation_id bigint,
    route_ref text,
    route_name text,
    operator_name text,
    original_status text,
    deletion_reason text,
    deletion_source_workflow text NOT NULL,
    deleted_by text,
    deleted_at timestamp with time zone DEFAULT now() NOT NULL,
    replaced_by_route_id uuid,
    restore_status text DEFAULT 'trashed'::text,
    restored_at timestamp with time zone,
    restored_by text,
    full_snapshot_jsonb jsonb NOT NULL,
    metadata_jsonb jsonb DEFAULT '{}'::jsonb,
    CONSTRAINT trash_items_restore_status_check CHECK ((restore_status = ANY (ARRAY['trashed'::text, 'restored'::text, 'purged'::text])))
);


--
-- Name: trash_summary; Type: VIEW; Schema: route_trash; Owner: -
--

CREATE VIEW route_trash.trash_summary AS
 SELECT trash_id,
    route_id,
    chosen_osm_relation_id,
    route_ref,
    route_name,
    original_status,
    deletion_reason,
    deletion_source_workflow,
    deleted_by,
    deleted_at,
    replaced_by_route_id,
    restore_status,
    restored_at
   FROM route_trash.trash_items t
  ORDER BY deleted_at DESC;


--
-- Name: discovery_runs; Type: TABLE; Schema: route_work; Owner: -
--

CREATE TABLE route_work.discovery_runs (
    run_id uuid DEFAULT gen_random_uuid() NOT NULL,
    route_name text NOT NULL,
    cooperative_name text,
    anchor_a_hint text,
    anchor_b_hint text,
    intermediate_hints text[] DEFAULT ARRAY[]::text[] NOT NULL,
    corridor_description text,
    source_catalog text,
    catalog_index integer,
    idempotency_key text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    scoring_mode text DEFAULT 'ensemble'::text,
    review_status text DEFAULT 'pending_review'::text NOT NULL,
    grounding_confidence real,
    anchor_a_matches jsonb DEFAULT '[]'::jsonb,
    anchor_b_matches jsonb DEFAULT '[]'::jsonb,
    unmatched_hints text[] DEFAULT ARRAY[]::text[] NOT NULL,
    corridor_geojson jsonb,
    corridor_length_km real,
    corridor_confidence real,
    skeleton_stops jsonb DEFAULT '[]'::jsonb,
    skeleton_stop_count integer DEFAULT 0,
    skeleton_gaps jsonb DEFAULT '[]'::jsonb,
    sequence_confidence real,
    geometry_geojson jsonb,
    geometry_length_km real,
    geometry_confidence real,
    geometry_derived_from text,
    metrics jsonb DEFAULT '{}'::jsonb,
    full_summary jsonb DEFAULT '{}'::jsonb,
    seed_payload jsonb DEFAULT '{}'::jsonb,
    source_notes text[] DEFAULT ARRAY[]::text[] NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    province text DEFAULT 'sample_region'::text NOT NULL
);


--
-- Name: discovery_review_v1; Type: VIEW; Schema: route_work; Owner: -
--

CREATE VIEW route_work.discovery_review_v1 AS
 SELECT run_id,
    route_name,
    cooperative_name,
    anchor_a_hint,
    anchor_b_hint,
    intermediate_hints,
    status,
    review_status,
    scoring_mode,
    grounding_confidence,
    corridor_length_km,
    corridor_confidence,
    skeleton_stop_count,
    sequence_confidence,
    geometry_length_km,
    geometry_confidence,
    COALESCE(((metrics ->> 'total_discovered_stops'::text))::integer, 0) AS discovered_stops,
    COALESCE(((metrics ->> 'total_gaps'::text))::integer, 0) AS total_gaps,
    COALESCE(((metrics ->> 'avg_sequence_gap_m'::text))::real, (0)::real) AS avg_gap_m,
    source_catalog,
    catalog_index,
    created_at,
    updated_at,
        CASE
            WHEN (status = 'failed'::text) THEN 'blocked'::text
            WHEN ((sequence_confidence >= (0.6)::double precision) AND (corridor_confidence >= (0.8)::double precision)) THEN 'strong'::text
            WHEN (sequence_confidence >= (0.3)::double precision) THEN 'moderate'::text
            WHEN (skeleton_stop_count >= 5) THEN 'weak_but_usable'::text
            ELSE 'weak'::text
        END AS strength_tier
   FROM route_work.discovery_runs r
  ORDER BY source_catalog, catalog_index;


--
-- Name: geometry_stop_recovery; Type: TABLE; Schema: route_work; Owner: -
--

CREATE TABLE route_work.geometry_stop_recovery (
    geometry_candidate_id uuid NOT NULL,
    set_id uuid NOT NULL,
    route_id uuid NOT NULL,
    stop_sequence_candidate_id uuid,
    original_stop_ids uuid[] DEFAULT ARRAY[]::uuid[] NOT NULL,
    original_stop_prior_seqs integer[] DEFAULT ARRAY[]::integer[] NOT NULL,
    recovered_stop_ids uuid[] DEFAULT ARRAY[]::uuid[] NOT NULL,
    ambiguous_nearby_stop_ids uuid[] DEFAULT ARRAY[]::uuid[] NOT NULL,
    rejected_nearby_stop_ids uuid[] DEFAULT ARRAY[]::uuid[] NOT NULL,
    enriched_stop_ids uuid[] DEFAULT ARRAY[]::uuid[] NOT NULL,
    insertion_proposals jsonb DEFAULT '[]'::jsonb NOT NULL,
    provenance jsonb DEFAULT '{}'::jsonb NOT NULL,
    summary_metrics jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: inverse_direction_status; Type: TABLE; Schema: route_work; Owner: -
--

CREATE TABLE route_work.inverse_direction_status (
    service_route_id uuid NOT NULL,
    direction_id smallint NOT NULL,
    anchor_route_id uuid,
    bound_route_id uuid,
    top_candidate_route_id uuid,
    inverse_status text DEFAULT 'unknown'::text NOT NULL,
    search_status text DEFAULT 'not_started'::text NOT NULL,
    manual_required boolean DEFAULT false NOT NULL,
    direction_ready boolean DEFAULT false NOT NULL,
    blocker_codes text[] DEFAULT ARRAY[]::text[] NOT NULL,
    blocker_messages text[] DEFAULT ARRAY[]::text[] NOT NULL,
    evidence_summary jsonb DEFAULT '{}'::jsonb NOT NULL,
    analysis_version text,
    last_evaluated_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    top_candidate_scores jsonb DEFAULT '{}'::jsonb NOT NULL,
    proposal_payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    proposal_source text,
    proposal_evaluated_at timestamp with time zone,
    search_request_payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    search_result_payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    dispatched_route_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    materialized_route_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    search_started_at timestamp with time zone,
    search_finished_at timestamp with time zone,
    search_error text,
    CONSTRAINT chk_inverse_direction_status_inverse_status CHECK ((inverse_status = ANY (ARRAY['unknown'::text, 'structurally_ready'::text, 'structurally_blocked'::text, 'reliable_opposite_candidate'::text, 'plausible_opposite_candidate'::text, 'no_candidate_found'::text]))),
    CONSTRAINT chk_inverse_direction_status_search_status CHECK ((search_status = ANY (ARRAY['not_started'::text, 'not_applicable'::text, 'pending'::text, 'dispatched'::text, 'discovered'::text, 'materialized'::text, 'no_results'::text, 'failed'::text]))),
    CONSTRAINT inverse_direction_status_direction_id_check CHECK ((direction_id = ANY (ARRAY[0, 1])))
);


--
-- Name: manual_sequence_drafts; Type: TABLE; Schema: route_work; Owner: -
--

CREATE TABLE route_work.manual_sequence_drafts (
    draft_id uuid DEFAULT gen_random_uuid() NOT NULL,
    route_id uuid,
    service_route_id uuid,
    direction_id smallint,
    source text DEFAULT 'manual_builder'::text NOT NULL,
    ordered_stop_ids uuid[] DEFAULT ARRAY[]::uuid[] NOT NULL,
    ordered_node_ids uuid[] DEFAULT ARRAY[]::uuid[] NOT NULL,
    ordered_coords jsonb DEFAULT '[]'::jsonb NOT NULL,
    is_loop boolean DEFAULT false NOT NULL,
    name_hint text,
    operator_hint text,
    variant_hint text,
    created_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT manual_sequence_drafts_direction_id_check CHECK ((direction_id = ANY (ARRAY[0, 1])))
);


--
-- Name: sequence_approvals; Type: TABLE; Schema: route_work; Owner: -
--

CREATE TABLE route_work.sequence_approvals (
    sequence_approval_id uuid DEFAULT gen_random_uuid() NOT NULL,
    route_id uuid NOT NULL,
    stop_sequence_set_id uuid,
    chosen_stop_sequence_candidate_id uuid,
    approval_status text DEFAULT 'approved'::text NOT NULL,
    approved_at timestamp with time zone,
    approved_by text,
    notes text,
    invalidated_at timestamp with time zone,
    invalidated_reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT chk_sequence_approvals_status CHECK ((approval_status = ANY (ARRAY['approved'::text, 'invalidated'::text])))
);


--
-- Name: service_route_approvals; Type: TABLE; Schema: route_work; Owner: -
--

CREATE TABLE route_work.service_route_approvals (
    approval_id uuid DEFAULT gen_random_uuid() NOT NULL,
    service_route_id uuid NOT NULL,
    approved_at timestamp with time zone DEFAULT now() NOT NULL,
    approved_by text,
    notes text
);


--
-- Name: v_direction_readiness; Type: VIEW; Schema: route_work; Owner: -
--

CREATE VIEW route_work.v_direction_readiness AS
 SELECT (sr.service_route_id)::text AS service_route_id,
    COALESCE(sr.route_ref, ''::text) AS route_short_name,
        CASE
            WHEN ((NULLIF(sr.route_ref, ''::text) IS NOT NULL) AND (NULLIF(sr.route_name, ''::text) IS NOT NULL)) THEN ((sr.route_ref || ' | '::text) || sr.route_name)
            WHEN (NULLIF(sr.route_ref, ''::text) IS NOT NULL) THEN sr.route_ref
            ELSE NULLIF(sr.route_name, ''::text)
        END AS route_label,
    COALESCE(sr.route_name, ''::text) AS route_name,
    COALESCE(sr.operator_name, ''::text) AS operator_name,
    slot.direction_id,
    (srd.route_id)::text AS logical_route_id,
    (COALESCE(ids.bound_route_id, srd.route_id))::text AS bound_route_id,
    (ids.anchor_route_id)::text AS anchor_route_id,
    (ids.top_candidate_route_id)::text AS top_candidate_route_id,
    COALESCE(srd.phase3_progress_step, 0) AS phase3_progress_step,
    COALESCE(srd.direction_approval_status, 'pending'::text) AS direction_approval_status,
    COALESCE(srd.geom_source, 'unknown'::text) AS geom_source,
    COALESCE(ids.inverse_status, 'unknown'::text) AS inverse_status,
    COALESCE(ids.search_status, 'not_started'::text) AS search_status,
    COALESCE(ids.search_request_payload, '{}'::jsonb) AS search_request_payload,
    COALESCE(ids.search_result_payload, '{}'::jsonb) AS search_result_payload,
    COALESCE(ids.dispatched_route_ids, '[]'::jsonb) AS dispatched_route_ids,
    COALESCE(ids.materialized_route_ids, '[]'::jsonb) AS materialized_route_ids,
    ids.search_started_at,
    ids.search_finished_at,
    ids.search_error,
    COALESCE(ids.manual_required, false) AS manual_required,
    COALESCE(ids.direction_ready, false) AS direction_ready,
    COALESCE(ids.blocker_codes, ARRAY[]::text[]) AS blocker_codes,
    COALESCE(ids.blocker_messages, ARRAY[]::text[]) AS blocker_messages,
    COALESCE(ids.evidence_summary, '{}'::jsonb) AS evidence_summary,
    COALESCE(ids.top_candidate_scores, '{}'::jsonb) AS top_candidate_scores,
    COALESCE(ids.proposal_payload, '{}'::jsonb) AS proposal_payload,
    ids.proposal_source,
    ids.proposal_evaluated_at,
    ids.analysis_version,
    ids.last_evaluated_at,
    ids.created_at,
    ids.updated_at
   FROM (((route_raw.service_routes sr
     CROSS JOIN ( VALUES (0), (1)) slot(direction_id))
     LEFT JOIN route_raw.service_route_directions srd ON (((srd.service_route_id = sr.service_route_id) AND (srd.direction_id = slot.direction_id))))
     LEFT JOIN route_work.inverse_direction_status ids ON (((ids.service_route_id = sr.service_route_id) AND (ids.direction_id = slot.direction_id))));


--
-- Name: valhalla_presets; Type: TABLE; Schema: route_work; Owner: -
--

CREATE TABLE route_work.valhalla_presets (
    preset_id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    params jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: route_evidence_records; Type: TABLE; Schema: semantics; Owner: -
--

CREATE TABLE semantics.route_evidence_records (
    record_id uuid DEFAULT gen_random_uuid() NOT NULL,
    source_type text NOT NULL,
    source_id text NOT NULL,
    route_id_hint uuid,
    route_ref text,
    route_name text,
    operator_name text,
    from_name text,
    to_name text,
    via text[],
    confidence_hint double precision DEFAULT 0.50 NOT NULL,
    bbox public.geometry(Polygon,4326),
    geom public.geometry(LineString,4326),
    raw jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: route_name_candidates; Type: TABLE; Schema: semantics; Owner: -
--

CREATE TABLE semantics.route_name_candidates (
    candidate_id uuid DEFAULT gen_random_uuid() NOT NULL,
    route_id uuid NOT NULL,
    run_id uuid DEFAULT gen_random_uuid() NOT NULL,
    rank_pos integer NOT NULL,
    route_name text NOT NULL,
    route_ref text,
    operator_name text,
    source_type text DEFAULT 'heuristic'::text NOT NULL,
    feature_snapshot_version text DEFAULT 'v1'::text NOT NULL,
    features jsonb DEFAULT '{}'::jsonb NOT NULL,
    heuristic_score double precision DEFAULT 0.0 NOT NULL,
    model_score double precision,
    final_score double precision DEFAULT 0.0 NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    generated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT route_name_candidates_rank_pos_check CHECK ((rank_pos >= 1))
);


--
-- Name: route_name_feedback; Type: TABLE; Schema: semantics; Owner: -
--

CREATE TABLE semantics.route_name_feedback (
    feedback_id uuid DEFAULT gen_random_uuid() NOT NULL,
    route_id uuid NOT NULL,
    candidate_id uuid NOT NULL,
    reviewer text DEFAULT 'console'::text NOT NULL,
    user_score integer NOT NULL,
    is_winner boolean DEFAULT false NOT NULL,
    feature_snapshot_version text DEFAULT 'v1'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT route_name_feedback_user_score_check CHECK (((user_score >= 1) AND (user_score <= 5)))
);


--
-- Name: route_name_seed_runs; Type: TABLE; Schema: semantics; Owner: -
--

CREATE TABLE semantics.route_name_seed_runs (
    seed_run_id uuid DEFAULT gen_random_uuid() NOT NULL,
    route_id uuid NOT NULL,
    seed_source text NOT NULL,
    chosen_osm_relation_id bigint,
    seed_route_name text,
    seed_route_ref text,
    seed_operator_name text,
    seed_from_name text,
    seed_to_name text,
    seed_payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT route_name_seed_runs_seed_source_check CHECK ((seed_source = ANY (ARRAY['osm_relation'::text, 'endpoint_fallback'::text])))
);


--
-- Name: v_phase4_seed; Type: VIEW; Schema: semantics; Owner: -
--

CREATE VIEW semantics.v_phase4_seed AS
 SELECT j.route_id,
    j.known_ref,
    COALESCE(j.chosen_osm_relation_id, r.osm_relation_id) AS osm_relation_id,
    (((r.overpass_json -> 'elements'::text) -> 0) -> 'tags'::text) AS tags,
    r.overpass_json AS raw_overpass_json
   FROM (route_raw.active_route_jobs j
     LEFT JOIN route_raw.osm_relations_raw r ON ((r.route_id = j.route_id)));


--
-- Name: v_routes_pending; Type: VIEW; Schema: semantics; Owner: -
--

CREATE VIEW semantics.v_routes_pending AS
 SELECT r.route_id,
    r.source,
    r.created_at,
    r.updated_at,
    s.route_name,
    s.route_ref,
    s.operator_name,
    s.route_aliases,
    s.landmark_tags,
    s.direction_semantics,
    s.naming_confidence,
    s.human_verified,
    s.semantics_updated_at,
    public.st_asewkt(r.geom) AS geometry_ewkt,
    r.service_route_id,
    r.direction_id
   FROM (route_prod.routes r
     LEFT JOIN route_prod.route_semantics s ON ((s.route_id = r.route_id)))
  WHERE (COALESCE(s.human_verified, false) = false);


--
-- Name: v_routes_search; Type: VIEW; Schema: semantics; Owner: -
--

CREATE VIEW semantics.v_routes_search AS
 SELECT r.route_id,
    s.route_name,
    s.route_ref,
    s.operator_name,
    s.route_aliases,
    s.landmark_tags,
    s.direction_semantics,
    s.naming_confidence,
    s.human_verified,
    s.semantics_updated_at,
    concat_ws(' '::text, COALESCE(s.route_name, ''::text), array_to_string(COALESCE(s.route_aliases, ARRAY[]::text[]), ' '::text), array_to_string(COALESCE(s.landmark_tags, ARRAY[]::text[]), ' '::text)) AS search_text,
    r.service_route_id,
    r.direction_id
   FROM (route_prod.routes r
     JOIN route_prod.route_semantics s ON ((s.route_id = r.route_id)));


--
-- Name: ai_bot_model_metrics metric_id; Type: DEFAULT; Schema: ai; Owner: -
--

ALTER TABLE ONLY ai.ai_bot_model_metrics ALTER COLUMN metric_id SET DEFAULT nextval('ai.ai_bot_model_metrics_metric_id_seq'::regclass);


--
-- Name: ai_bot_run_logs log_id; Type: DEFAULT; Schema: ai; Owner: -
--

ALTER TABLE ONLY ai.ai_bot_run_logs ALTER COLUMN log_id SET DEFAULT nextval('ai.ai_bot_run_logs_log_id_seq'::regclass);


--
-- Name: ai_bot_train_events event_id; Type: DEFAULT; Schema: ai; Owner: -
--

ALTER TABLE ONLY ai.ai_bot_train_events ALTER COLUMN event_id SET DEFAULT nextval('ai.ai_bot_train_events_event_id_seq'::regclass);


--
-- Name: ai_training_dataset row_id; Type: DEFAULT; Schema: ai; Owner: -
--

ALTER TABLE ONLY ai.ai_training_dataset ALTER COLUMN row_id SET DEFAULT nextval('ai.ai_training_dataset_row_id_seq'::regclass);


--
-- Name: diagnostics id; Type: DEFAULT; Schema: automation; Owner: -
--

ALTER TABLE ONLY automation.diagnostics ALTER COLUMN id SET DEFAULT nextval('automation.diagnostics_id_seq'::regclass);


--
-- Name: patches id; Type: DEFAULT; Schema: automation; Owner: -
--

ALTER TABLE ONLY automation.patches ALTER COLUMN id SET DEFAULT nextval('automation.patches_id_seq'::regclass);


--
-- Name: route_layover_policy id; Type: DEFAULT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.route_layover_policy ALTER COLUMN id SET DEFAULT nextval('catalog.route_layover_policy_id_seq'::regclass);


--
-- Name: route_schedule_profile id; Type: DEFAULT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.route_schedule_profile ALTER COLUMN id SET DEFAULT nextval('catalog.route_schedule_profile_id_seq'::regclass);


--
-- Name: route_service_days id; Type: DEFAULT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.route_service_days ALTER COLUMN id SET DEFAULT nextval('catalog.route_service_days_id_seq'::regclass);


--
-- Name: route_service_exceptions id; Type: DEFAULT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.route_service_exceptions ALTER COLUMN id SET DEFAULT nextval('catalog.route_service_exceptions_id_seq'::regclass);


--
-- Name: runtime_congestion_hotspots id; Type: DEFAULT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.runtime_congestion_hotspots ALTER COLUMN id SET DEFAULT nextval('gtfs_work.runtime_congestion_hotspots_id_seq'::regclass);


--
-- Name: runtime_route_priors id; Type: DEFAULT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.runtime_route_priors ALTER COLUMN id SET DEFAULT nextval('gtfs_work.runtime_route_priors_id_seq'::regclass);


--
-- Name: direction_construction_audit id; Type: DEFAULT; Schema: node_prod; Owner: -
--

ALTER TABLE ONLY node_prod.direction_construction_audit ALTER COLUMN id SET DEFAULT nextval('node_prod.direction_construction_audit_id_seq'::regclass);


--
-- Name: precision_snap_audit id; Type: DEFAULT; Schema: node_prod; Owner: -
--

ALTER TABLE ONLY node_prod.precision_snap_audit ALTER COLUMN id SET DEFAULT nextval('node_prod.precision_snap_audit_id_seq'::regclass);


--
-- Name: stop_treatment_log id; Type: DEFAULT; Schema: node_prod; Owner: -
--

ALTER TABLE ONLY node_prod.stop_treatment_log ALTER COLUMN id SET DEFAULT nextval('node_prod.stop_treatment_log_id_seq'::regclass);


--
-- Name: synthesis_events id; Type: DEFAULT; Schema: node_prod; Owner: -
--

ALTER TABLE ONLY node_prod.synthesis_events ALTER COLUMN id SET DEFAULT nextval('node_prod.synthesis_events_id_seq'::regclass);


--
-- Name: cleanup_override_audit id; Type: DEFAULT; Schema: route_prod; Owner: -
--

ALTER TABLE ONLY route_prod.cleanup_override_audit ALTER COLUMN id SET DEFAULT nextval('route_prod.cleanup_override_audit_id_seq'::regclass);


--
-- Name: cleanup_v2_to_postapproval_resets id; Type: DEFAULT; Schema: route_prod; Owner: -
--

ALTER TABLE ONLY route_prod.cleanup_v2_to_postapproval_resets ALTER COLUMN id SET DEFAULT nextval('route_prod.cleanup_v2_to_postapproval_resets_id_seq'::regclass);


--
-- Name: refill_audit id; Type: DEFAULT; Schema: route_prod; Owner: -
--

ALTER TABLE ONLY route_prod.refill_audit ALTER COLUMN id SET DEFAULT nextval('route_prod.refill_audit_id_seq'::regclass);


--
-- Name: routes_audit audit_id; Type: DEFAULT; Schema: route_prod; Owner: -
--

ALTER TABLE ONLY route_prod.routes_audit ALTER COLUMN audit_id SET DEFAULT nextval('route_prod.routes_audit_audit_id_seq'::regclass);


--
-- Name: ai_agent_schedule ai_agent_schedule_pkey; Type: CONSTRAINT; Schema: ai; Owner: -
--

ALTER TABLE ONLY ai.ai_agent_schedule
    ADD CONSTRAINT ai_agent_schedule_pkey PRIMARY KEY (schedule_id);


--
-- Name: ai_bot_model_metrics ai_bot_model_metrics_pkey; Type: CONSTRAINT; Schema: ai; Owner: -
--

ALTER TABLE ONLY ai.ai_bot_model_metrics
    ADD CONSTRAINT ai_bot_model_metrics_pkey PRIMARY KEY (metric_id);


--
-- Name: ai_bot_run_logs ai_bot_run_logs_pkey; Type: CONSTRAINT; Schema: ai; Owner: -
--

ALTER TABLE ONLY ai.ai_bot_run_logs
    ADD CONSTRAINT ai_bot_run_logs_pkey PRIMARY KEY (log_id);


--
-- Name: ai_bot_train_events ai_bot_train_events_pkey; Type: CONSTRAINT; Schema: ai; Owner: -
--

ALTER TABLE ONLY ai.ai_bot_train_events
    ADD CONSTRAINT ai_bot_train_events_pkey PRIMARY KEY (event_id);


--
-- Name: ai_escalations ai_escalations_pkey; Type: CONSTRAINT; Schema: ai; Owner: -
--

ALTER TABLE ONLY ai.ai_escalations
    ADD CONSTRAINT ai_escalations_pkey PRIMARY KEY (escalation_id);


--
-- Name: ai_label_events ai_label_events_pkey; Type: CONSTRAINT; Schema: ai; Owner: -
--

ALTER TABLE ONLY ai.ai_label_events
    ADD CONSTRAINT ai_label_events_pkey PRIMARY KEY (label_event_id);


--
-- Name: ai_model_registry ai_model_registry_pkey; Type: CONSTRAINT; Schema: ai; Owner: -
--

ALTER TABLE ONLY ai.ai_model_registry
    ADD CONSTRAINT ai_model_registry_pkey PRIMARY KEY (model_id);


--
-- Name: ai_suggestions ai_suggestions_pkey; Type: CONSTRAINT; Schema: ai; Owner: -
--

ALTER TABLE ONLY ai.ai_suggestions
    ADD CONSTRAINT ai_suggestions_pkey PRIMARY KEY (suggestion_id);


--
-- Name: ai_training_dataset ai_training_dataset_pkey; Type: CONSTRAINT; Schema: ai; Owner: -
--

ALTER TABLE ONLY ai.ai_training_dataset
    ADD CONSTRAINT ai_training_dataset_pkey PRIMARY KEY (row_id);


--
-- Name: diagnostics diagnostics_pkey; Type: CONSTRAINT; Schema: automation; Owner: -
--

ALTER TABLE ONLY automation.diagnostics
    ADD CONSTRAINT diagnostics_pkey PRIMARY KEY (id);


--
-- Name: patches patches_pkey; Type: CONSTRAINT; Schema: automation; Owner: -
--

ALTER TABLE ONLY automation.patches
    ADD CONSTRAINT patches_pkey PRIMARY KEY (id);


--
-- Name: route_layover_policy route_layover_policy_pkey; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.route_layover_policy
    ADD CONSTRAINT route_layover_policy_pkey PRIMARY KEY (id);


--
-- Name: route_layover_policy route_layover_policy_route_id_applies_to_pattern_key; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.route_layover_policy
    ADD CONSTRAINT route_layover_policy_route_id_applies_to_pattern_key UNIQUE (route_id, applies_to_pattern);


--
-- Name: route_schedule_profile route_schedule_profile_pkey; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.route_schedule_profile
    ADD CONSTRAINT route_schedule_profile_pkey PRIMARY KEY (id);


--
-- Name: route_schedule_profile route_schedule_profile_route_id_direction_id_service_patter_key; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.route_schedule_profile
    ADD CONSTRAINT route_schedule_profile_route_id_direction_id_service_patter_key UNIQUE (route_id, direction_id, service_pattern_id, window_start);


--
-- Name: route_semantics route_semantics_pkey; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.route_semantics
    ADD CONSTRAINT route_semantics_pkey PRIMARY KEY (route_id);


--
-- Name: route_service_days route_service_days_pkey; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.route_service_days
    ADD CONSTRAINT route_service_days_pkey PRIMARY KEY (id);


--
-- Name: route_service_days route_service_days_route_id_service_pattern_id_key; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.route_service_days
    ADD CONSTRAINT route_service_days_route_id_service_pattern_id_key UNIQUE (route_id, service_pattern_id);


--
-- Name: route_service_exceptions route_service_exceptions_pkey; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.route_service_exceptions
    ADD CONSTRAINT route_service_exceptions_pkey PRIMARY KEY (id);


--
-- Name: route_service_exceptions route_service_exceptions_route_id_exception_date_key; Type: CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.route_service_exceptions
    ADD CONSTRAINT route_service_exceptions_route_id_exception_date_key UNIQUE (route_id, exception_date);


--
-- Name: api_keys api_keys_key_hash_key; Type: CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.api_keys
    ADD CONSTRAINT api_keys_key_hash_key UNIQUE (key_hash);


--
-- Name: api_keys api_keys_pkey; Type: CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.api_keys
    ADD CONSTRAINT api_keys_pkey PRIMARY KEY (key_id);


--
-- Name: approval_queue approval_queue_pkey; Type: CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.approval_queue
    ADD CONSTRAINT approval_queue_pkey PRIMARY KEY (approval_id);


--
-- Name: audit_events audit_events_pkey; Type: CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.audit_events
    ADD CONSTRAINT audit_events_pkey PRIMARY KEY (event_id);


--
-- Name: email_outbox email_outbox_pkey; Type: CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.email_outbox
    ADD CONSTRAINT email_outbox_pkey PRIMARY KEY (email_id);


--
-- Name: email_verification_tokens email_verification_tokens_pkey; Type: CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.email_verification_tokens
    ADD CONSTRAINT email_verification_tokens_pkey PRIMARY KEY (token_id);


--
-- Name: email_verification_tokens email_verification_tokens_token_key; Type: CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.email_verification_tokens
    ADD CONSTRAINT email_verification_tokens_token_key UNIQUE (token);


--
-- Name: export_jobs export_jobs_pkey; Type: CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.export_jobs
    ADD CONSTRAINT export_jobs_pkey PRIMARY KEY (job_id);


--
-- Name: orchestrator_sessions orchestrator_sessions_pkey; Type: CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.orchestrator_sessions
    ADD CONSTRAINT orchestrator_sessions_pkey PRIMARY KEY (session_id);


--
-- Name: orchestrator_steps orchestrator_steps_pkey; Type: CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.orchestrator_steps
    ADD CONSTRAINT orchestrator_steps_pkey PRIMARY KEY (step_id);


--
-- Name: phase_decisions phase_decisions_pkey; Type: CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.phase_decisions
    ADD CONSTRAINT phase_decisions_pkey PRIMARY KEY (decision_id);


--
-- Name: sessions sessions_pkey; Type: CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.sessions
    ADD CONSTRAINT sessions_pkey PRIMARY KEY (session_id);


--
-- Name: sessions sessions_token_hash_key; Type: CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.sessions
    ADD CONSTRAINT sessions_token_hash_key UNIQUE (token_hash);


--
-- Name: sync_events sync_events_pkey; Type: CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.sync_events
    ADD CONSTRAINT sync_events_pkey PRIMARY KEY (sync_id);


--
-- Name: orchestrator_steps uq_orch_step_session_idx; Type: CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.orchestrator_steps
    ADD CONSTRAINT uq_orch_step_session_idx UNIQUE (session_id, step_idx);


--
-- Name: users users_email_key; Type: CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.users
    ADD CONSTRAINT users_email_key UNIQUE (email);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (user_id);


--
-- Name: workspace_state workspace_state_pkey; Type: CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.workspace_state
    ADD CONSTRAINT workspace_state_pkey PRIMARY KEY (user_id);


--
-- Name: node_place_map node_place_map_pkey; Type: CONSTRAINT; Schema: geo_prod; Owner: -
--

ALTER TABLE ONLY geo_prod.node_place_map
    ADD CONSTRAINT node_place_map_pkey PRIMARY KEY (node_id);


--
-- Name: place_alias_embeddings place_alias_embeddings_pkey; Type: CONSTRAINT; Schema: geo_prod; Owner: -
--

ALTER TABLE ONLY geo_prod.place_alias_embeddings
    ADD CONSTRAINT place_alias_embeddings_pkey PRIMARY KEY (alias_id);


--
-- Name: place_aliases place_aliases_pkey; Type: CONSTRAINT; Schema: geo_prod; Owner: -
--

ALTER TABLE ONLY geo_prod.place_aliases
    ADD CONSTRAINT place_aliases_pkey PRIMARY KEY (alias_id);


--
-- Name: place_embeddings place_embeddings_pkey; Type: CONSTRAINT; Schema: geo_prod; Owner: -
--

ALTER TABLE ONLY geo_prod.place_embeddings
    ADD CONSTRAINT place_embeddings_pkey PRIMARY KEY (place_id);


--
-- Name: place_history place_history_pkey; Type: CONSTRAINT; Schema: geo_prod; Owner: -
--

ALTER TABLE ONLY geo_prod.place_history
    ADD CONSTRAINT place_history_pkey PRIMARY KEY (history_id);


--
-- Name: places places_pkey; Type: CONSTRAINT; Schema: geo_prod; Owner: -
--

ALTER TABLE ONLY geo_prod.places
    ADD CONSTRAINT places_pkey PRIMARY KEY (place_id);


--
-- Name: place_aliases uq_geo_place_aliases_norm_global; Type: CONSTRAINT; Schema: geo_prod; Owner: -
--

ALTER TABLE ONLY geo_prod.place_aliases
    ADD CONSTRAINT uq_geo_place_aliases_norm_global UNIQUE (normalized_alias, place_id);


--
-- Name: place_aliases uq_geo_place_aliases_place_norm; Type: CONSTRAINT; Schema: geo_prod; Owner: -
--

ALTER TABLE ONLY geo_prod.place_aliases
    ADD CONSTRAINT uq_geo_place_aliases_place_norm UNIQUE (place_id, normalized_alias);


--
-- Name: extract_runs extract_runs_pkey; Type: CONSTRAINT; Schema: geo_raw; Owner: -
--

ALTER TABLE ONLY geo_raw.extract_runs
    ADD CONSTRAINT extract_runs_pkey PRIMARY KEY (extract_run_id);


--
-- Name: alias_candidates alias_candidates_pkey; Type: CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.alias_candidates
    ADD CONSTRAINT alias_candidates_pkey PRIMARY KEY (alias_candidate_id);


--
-- Name: model_registry model_registry_pkey; Type: CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.model_registry
    ADD CONSTRAINT model_registry_pkey PRIMARY KEY (model_name);


--
-- Name: node_geo_context node_geo_context_pkey; Type: CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.node_geo_context
    ADD CONSTRAINT node_geo_context_pkey PRIMARY KEY (extract_run_id, node_id);


--
-- Name: node_place_map_work node_place_map_work_pkey; Type: CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.node_place_map_work
    ADD CONSTRAINT node_place_map_work_pkey PRIMARY KEY (place_set_id, node_id);


--
-- Name: place_candidate_sets place_candidate_sets_pkey; Type: CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.place_candidate_sets
    ADD CONSTRAINT place_candidate_sets_pkey PRIMARY KEY (place_set_id);


--
-- Name: place_candidates place_candidates_pkey; Type: CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.place_candidates
    ADD CONSTRAINT place_candidates_pkey PRIMARY KEY (place_candidate_id);


--
-- Name: place_name_candidates place_name_candidates_pkey; Type: CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.place_name_candidates
    ADD CONSTRAINT place_name_candidates_pkey PRIMARY KEY (name_candidate_id);


--
-- Name: place_name_feedback place_name_feedback_pkey; Type: CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.place_name_feedback
    ADD CONSTRAINT place_name_feedback_pkey PRIMARY KEY (feedback_id);


--
-- Name: place_set_metrics place_set_metrics_pkey; Type: CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.place_set_metrics
    ADD CONSTRAINT place_set_metrics_pkey PRIMARY KEY (place_set_id);


--
-- Name: poi_stop_feedback poi_stop_feedback_pkey; Type: CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.poi_stop_feedback
    ADD CONSTRAINT poi_stop_feedback_pkey PRIMARY KEY (feedback_id);


--
-- Name: selection_log selection_log_pkey; Type: CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.selection_log
    ADD CONSTRAINT selection_log_pkey PRIMARY KEY (selection_id);


--
-- Name: alias_candidates uq_geo_alias_candidates_place_alias; Type: CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.alias_candidates
    ADD CONSTRAINT uq_geo_alias_candidates_place_alias UNIQUE (place_candidate_id, alias);


--
-- Name: place_name_candidates uq_place_name_candidate_norm; Type: CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.place_name_candidates
    ADD CONSTRAINT uq_place_name_candidate_norm UNIQUE (place_candidate_id, candidate_name_norm);


--
-- Name: gtfs_artifacts gtfs_artifacts_pkey; Type: CONSTRAINT; Schema: gtfs; Owner: -
--

ALTER TABLE ONLY gtfs.gtfs_artifacts
    ADD CONSTRAINT gtfs_artifacts_pkey PRIMARY KEY (artifact_id);


--
-- Name: gtfs_audit_log gtfs_audit_log_pkey; Type: CONSTRAINT; Schema: gtfs; Owner: -
--

ALTER TABLE ONLY gtfs.gtfs_audit_log
    ADD CONSTRAINT gtfs_audit_log_pkey PRIMARY KEY (audit_id);


--
-- Name: gtfs_notifications gtfs_notifications_pkey; Type: CONSTRAINT; Schema: gtfs; Owner: -
--

ALTER TABLE ONLY gtfs.gtfs_notifications
    ADD CONSTRAINT gtfs_notifications_pkey PRIMARY KEY (notification_id);


--
-- Name: feed_versions feed_versions_export_run_id_key; Type: CONSTRAINT; Schema: gtfs_prod; Owner: -
--

ALTER TABLE ONLY gtfs_prod.feed_versions
    ADD CONSTRAINT feed_versions_export_run_id_key UNIQUE (export_run_id);


--
-- Name: feed_versions feed_versions_pkey; Type: CONSTRAINT; Schema: gtfs_prod; Owner: -
--

ALTER TABLE ONLY gtfs_prod.feed_versions
    ADD CONSTRAINT feed_versions_pkey PRIMARY KEY (feed_version_id);


--
-- Name: agency_catalog agency_catalog_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.agency_catalog
    ADD CONSTRAINT agency_catalog_pkey PRIMARY KEY (agency_id);


--
-- Name: agency_match_requests agency_match_requests_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.agency_match_requests
    ADD CONSTRAINT agency_match_requests_pkey PRIMARY KEY (request_id);


--
-- Name: calendar_exceptions calendar_exceptions_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.calendar_exceptions
    ADD CONSTRAINT calendar_exceptions_pkey PRIMARY KEY (exception_id);


--
-- Name: export_runs export_runs_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.export_runs
    ADD CONSTRAINT export_runs_pkey PRIMARY KEY (export_run_id);


--
-- Name: gtfs_agency gtfs_agency_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.gtfs_agency
    ADD CONSTRAINT gtfs_agency_pkey PRIMARY KEY (export_run_id, agency_id);


--
-- Name: gtfs_builds gtfs_builds_export_run_id_key; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.gtfs_builds
    ADD CONSTRAINT gtfs_builds_export_run_id_key UNIQUE (export_run_id);


--
-- Name: gtfs_builds gtfs_builds_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.gtfs_builds
    ADD CONSTRAINT gtfs_builds_pkey PRIMARY KEY (gtfs_id);


--
-- Name: gtfs_calendar_dates gtfs_calendar_dates_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.gtfs_calendar_dates
    ADD CONSTRAINT gtfs_calendar_dates_pkey PRIMARY KEY (export_run_id, service_id, date);


--
-- Name: gtfs_calendar gtfs_calendar_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.gtfs_calendar
    ADD CONSTRAINT gtfs_calendar_pkey PRIMARY KEY (export_run_id, service_id);


--
-- Name: gtfs_frequencies gtfs_frequencies_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.gtfs_frequencies
    ADD CONSTRAINT gtfs_frequencies_pkey PRIMARY KEY (export_run_id, trip_id, start_time);


--
-- Name: gtfs_loadings gtfs_loadings_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.gtfs_loadings
    ADD CONSTRAINT gtfs_loadings_pkey PRIMARY KEY (loading_id);


--
-- Name: gtfs_overrides gtfs_overrides_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.gtfs_overrides
    ADD CONSTRAINT gtfs_overrides_pkey PRIMARY KEY (override_id);


--
-- Name: gtfs_routes gtfs_routes_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.gtfs_routes
    ADD CONSTRAINT gtfs_routes_pkey PRIMARY KEY (export_run_id, route_id);


--
-- Name: gtfs_shapes gtfs_shapes_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.gtfs_shapes
    ADD CONSTRAINT gtfs_shapes_pkey PRIMARY KEY (export_run_id, shape_id, shape_pt_sequence);


--
-- Name: gtfs_stop_times gtfs_stop_times_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.gtfs_stop_times
    ADD CONSTRAINT gtfs_stop_times_pkey PRIMARY KEY (export_run_id, trip_id, stop_sequence);


--
-- Name: gtfs_stops gtfs_stops_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.gtfs_stops
    ADD CONSTRAINT gtfs_stops_pkey PRIMARY KEY (export_run_id, stop_id);


--
-- Name: gtfs_trips gtfs_trips_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.gtfs_trips
    ADD CONSTRAINT gtfs_trips_pkey PRIMARY KEY (export_run_id, trip_id);


--
-- Name: revision_events revision_events_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.revision_events
    ADD CONSTRAINT revision_events_pkey PRIMARY KEY (event_id);


--
-- Name: route_agency_links route_agency_links_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.route_agency_links
    ADD CONSTRAINT route_agency_links_pkey PRIMARY KEY (route_id);


--
-- Name: route_runtime_estimate_bindings route_runtime_estimate_bindings_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.route_runtime_estimate_bindings
    ADD CONSTRAINT route_runtime_estimate_bindings_pkey PRIMARY KEY (route_id, direction_id);


--
-- Name: route_schedule_profiles route_schedule_profiles_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.route_schedule_profiles
    ADD CONSTRAINT route_schedule_profiles_pkey PRIMARY KEY (profile_id);


--
-- Name: route_schedule_profiles route_schedule_profiles_route_id_direction_id_service_name_key; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.route_schedule_profiles
    ADD CONSTRAINT route_schedule_profiles_route_id_direction_id_service_name_key UNIQUE (route_id, direction_id, service_name);


--
-- Name: runtime_catalog_items runtime_catalog_items_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.runtime_catalog_items
    ADD CONSTRAINT runtime_catalog_items_pkey PRIMARY KEY (catalog_key, item_code);


--
-- Name: runtime_congestion_hotspots runtime_congestion_hotspots_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.runtime_congestion_hotspots
    ADD CONSTRAINT runtime_congestion_hotspots_pkey PRIMARY KEY (id);


--
-- Name: runtime_route_estimates runtime_route_estimates_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.runtime_route_estimates
    ADD CONSTRAINT runtime_route_estimates_pkey PRIMARY KEY (estimate_id);


--
-- Name: runtime_route_leg_features runtime_route_leg_features_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.runtime_route_leg_features
    ADD CONSTRAINT runtime_route_leg_features_pkey PRIMARY KEY (leg_feature_id);


--
-- Name: runtime_route_priors runtime_route_priors_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.runtime_route_priors
    ADD CONSTRAINT runtime_route_priors_pkey PRIMARY KEY (id);


--
-- Name: runtime_route_priors runtime_route_priors_route_id_direction_id_time_period_key; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.runtime_route_priors
    ADD CONSTRAINT runtime_route_priors_route_id_direction_id_time_period_key UNIQUE (route_id, direction_id, time_period);


--
-- Name: service_windows service_windows_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.service_windows
    ADD CONSTRAINT service_windows_pkey PRIMARY KEY (window_id);


--
-- Name: trip_departures trip_departures_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.trip_departures
    ADD CONSTRAINT trip_departures_pkey PRIMARY KEY (export_run_id, trip_id);


--
-- Name: upload_runs upload_runs_pkey; Type: CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.upload_runs
    ADD CONSTRAINT upload_runs_pkey PRIMARY KEY (export_run_id);


--
-- Name: direction_construction_audit direction_construction_audit_pkey; Type: CONSTRAINT; Schema: node_prod; Owner: -
--

ALTER TABLE ONLY node_prod.direction_construction_audit
    ADD CONSTRAINT direction_construction_audit_pkey PRIMARY KEY (id);


--
-- Name: nodes nodes_pkey; Type: CONSTRAINT; Schema: node_prod; Owner: -
--

ALTER TABLE ONLY node_prod.nodes
    ADD CONSTRAINT nodes_pkey PRIMARY KEY (node_id);


--
-- Name: precision_snap_audit precision_snap_audit_pkey; Type: CONSTRAINT; Schema: node_prod; Owner: -
--

ALTER TABLE ONLY node_prod.precision_snap_audit
    ADD CONSTRAINT precision_snap_audit_pkey PRIMARY KEY (id);


--
-- Name: stop_treatment_log stop_treatment_log_pkey; Type: CONSTRAINT; Schema: node_prod; Owner: -
--

ALTER TABLE ONLY node_prod.stop_treatment_log
    ADD CONSTRAINT stop_treatment_log_pkey PRIMARY KEY (id);


--
-- Name: stop_treatment_log stop_treatment_log_treatment_id_key; Type: CONSTRAINT; Schema: node_prod; Owner: -
--

ALTER TABLE ONLY node_prod.stop_treatment_log
    ADD CONSTRAINT stop_treatment_log_treatment_id_key UNIQUE (treatment_id);


--
-- Name: synthesis_events synthesis_events_pkey; Type: CONSTRAINT; Schema: node_prod; Owner: -
--

ALTER TABLE ONLY node_prod.synthesis_events
    ADD CONSTRAINT synthesis_events_pkey PRIMARY KEY (id);


--
-- Name: overpass_actions overpass_actions_pkey; Type: CONSTRAINT; Schema: node_raw; Owner: -
--

ALTER TABLE ONLY node_raw.overpass_actions
    ADD CONSTRAINT overpass_actions_pkey PRIMARY KEY (action_id);


--
-- Name: overpass_queries overpass_queries_pkey; Type: CONSTRAINT; Schema: node_raw; Owner: -
--

ALTER TABLE ONLY node_raw.overpass_queries
    ADD CONSTRAINT overpass_queries_pkey PRIMARY KEY (query_id);


--
-- Name: overpass_runs overpass_runs_pkey; Type: CONSTRAINT; Schema: node_raw; Owner: -
--

ALTER TABLE ONLY node_raw.overpass_runs
    ADD CONSTRAINT overpass_runs_pkey PRIMARY KEY (run_id);


--
-- Name: overpass_elements uq_node_overpass_elements_run_osm; Type: CONSTRAINT; Schema: node_raw; Owner: -
--

ALTER TABLE ONLY node_raw.overpass_elements
    ADD CONSTRAINT uq_node_overpass_elements_run_osm UNIQUE (run_id, osm_type, osm_id);


--
-- Name: bandit_state bandit_state_pkey; Type: CONSTRAINT; Schema: node_work; Owner: -
--

ALTER TABLE ONLY node_work.bandit_state
    ADD CONSTRAINT bandit_state_pkey PRIMARY KEY (key);


--
-- Name: node_candidate_sets node_candidate_sets_pkey; Type: CONSTRAINT; Schema: node_work; Owner: -
--

ALTER TABLE ONLY node_work.node_candidate_sets
    ADD CONSTRAINT node_candidate_sets_pkey PRIMARY KEY (node_set_id);


--
-- Name: node_candidates node_candidates_pkey; Type: CONSTRAINT; Schema: node_work; Owner: -
--

ALTER TABLE ONLY node_work.node_candidates
    ADD CONSTRAINT node_candidates_pkey PRIMARY KEY (node_candidate_id);


--
-- Name: node_clusters node_clusters_pkey; Type: CONSTRAINT; Schema: node_work; Owner: -
--

ALTER TABLE ONLY node_work.node_clusters
    ADD CONSTRAINT node_clusters_pkey PRIMARY KEY (node_set_id, node_candidate_id);


--
-- Name: node_features node_features_pkey; Type: CONSTRAINT; Schema: node_work; Owner: -
--

ALTER TABLE ONLY node_work.node_features
    ADD CONSTRAINT node_features_pkey PRIMARY KEY (node_candidate_id);


--
-- Name: node_review_requests node_review_requests_pkey; Type: CONSTRAINT; Schema: node_work; Owner: -
--

ALTER TABLE ONLY node_work.node_review_requests
    ADD CONSTRAINT node_review_requests_pkey PRIMARY KEY (request_id);


--
-- Name: nodes_resolved nodes_resolved_pkey; Type: CONSTRAINT; Schema: node_work; Owner: -
--

ALTER TABLE ONLY node_work.nodes_resolved
    ADD CONSTRAINT nodes_resolved_pkey PRIMARY KEY (node_id);


--
-- Name: node_candidates uq_node_candidates_set_osm; Type: CONSTRAINT; Schema: node_work; Owner: -
--

ALTER TABLE ONLY node_work.node_candidates
    ADD CONSTRAINT uq_node_candidates_set_osm UNIQUE (node_set_id, osm_type, osm_id);


--
-- Name: nodes_resolved uq_nodes_resolved_set_cluster; Type: CONSTRAINT; Schema: node_work; Owner: -
--

ALTER TABLE ONLY node_work.nodes_resolved
    ADD CONSTRAINT uq_nodes_resolved_set_cluster UNIQUE (node_set_id, cluster_id);


--
-- Name: approval_queue approval_queue_pkey; Type: CONSTRAINT; Schema: route_prod; Owner: -
--

ALTER TABLE ONLY route_prod.approval_queue
    ADD CONSTRAINT approval_queue_pkey PRIMARY KEY (queue_id);


--
-- Name: cleanup_override_audit cleanup_override_audit_pkey; Type: CONSTRAINT; Schema: route_prod; Owner: -
--

ALTER TABLE ONLY route_prod.cleanup_override_audit
    ADD CONSTRAINT cleanup_override_audit_pkey PRIMARY KEY (id);


--
-- Name: cleanup_v2_to_postapproval_resets cleanup_v2_to_postapproval_resets_pkey; Type: CONSTRAINT; Schema: route_prod; Owner: -
--

ALTER TABLE ONLY route_prod.cleanup_v2_to_postapproval_resets
    ADD CONSTRAINT cleanup_v2_to_postapproval_resets_pkey PRIMARY KEY (id);


--
-- Name: dr_batch_dependencies dr_batch_dependencies_dr_batch_id_route_id_gap_idx_key; Type: CONSTRAINT; Schema: route_prod; Owner: -
--

ALTER TABLE ONLY route_prod.dr_batch_dependencies
    ADD CONSTRAINT dr_batch_dependencies_dr_batch_id_route_id_gap_idx_key UNIQUE (dr_batch_id, route_id, gap_idx);


--
-- Name: dr_batch_dependencies dr_batch_dependencies_pkey; Type: CONSTRAINT; Schema: route_prod; Owner: -
--

ALTER TABLE ONLY route_prod.dr_batch_dependencies
    ADD CONSTRAINT dr_batch_dependencies_pkey PRIMARY KEY (dependency_id);


--
-- Name: fix_reports fix_reports_pkey; Type: CONSTRAINT; Schema: route_prod; Owner: -
--

ALTER TABLE ONLY route_prod.fix_reports
    ADD CONSTRAINT fix_reports_pkey PRIMARY KEY (report_id);


--
-- Name: re_entry_queue re_entry_queue_pkey; Type: CONSTRAINT; Schema: route_prod; Owner: -
--

ALTER TABLE ONLY route_prod.re_entry_queue
    ADD CONSTRAINT re_entry_queue_pkey PRIMARY KEY (queue_id);


--
-- Name: refill_audit refill_audit_pkey; Type: CONSTRAINT; Schema: route_prod; Owner: -
--

ALTER TABLE ONLY route_prod.refill_audit
    ADD CONSTRAINT refill_audit_pkey PRIMARY KEY (id);


--
-- Name: route_semantics route_semantics_pkey; Type: CONSTRAINT; Schema: route_prod; Owner: -
--

ALTER TABLE ONLY route_prod.route_semantics
    ADD CONSTRAINT route_semantics_pkey PRIMARY KEY (route_id);


--
-- Name: routes_audit routes_audit_pkey; Type: CONSTRAINT; Schema: route_prod; Owner: -
--

ALTER TABLE ONLY route_prod.routes_audit
    ADD CONSTRAINT routes_audit_pkey PRIMARY KEY (audit_id);


--
-- Name: routes routes_pkey; Type: CONSTRAINT; Schema: route_prod; Owner: -
--

ALTER TABLE ONLY route_prod.routes
    ADD CONSTRAINT routes_pkey PRIMARY KEY (route_id, version);


--
-- Name: routes routes_uq_route_id; Type: CONSTRAINT; Schema: route_prod; Owner: -
--

ALTER TABLE ONLY route_prod.routes
    ADD CONSTRAINT routes_uq_route_id UNIQUE (route_id);


--
-- Name: osm_relations_raw osm_relations_raw_pkey; Type: CONSTRAINT; Schema: route_raw; Owner: -
--

ALTER TABLE ONLY route_raw.osm_relations_raw
    ADD CONSTRAINT osm_relations_raw_pkey PRIMARY KEY (route_id);


--
-- Name: relation_candidates relation_candidates_pkey; Type: CONSTRAINT; Schema: route_raw; Owner: -
--

ALTER TABLE ONLY route_raw.relation_candidates
    ADD CONSTRAINT relation_candidates_pkey PRIMARY KEY (candidate_id);


--
-- Name: route_jobs route_jobs_pkey; Type: CONSTRAINT; Schema: route_raw; Owner: -
--

ALTER TABLE ONLY route_raw.route_jobs
    ADD CONSTRAINT route_jobs_pkey PRIMARY KEY (route_id);


--
-- Name: service_route_directions service_route_directions_pkey; Type: CONSTRAINT; Schema: route_raw; Owner: -
--

ALTER TABLE ONLY route_raw.service_route_directions
    ADD CONSTRAINT service_route_directions_pkey PRIMARY KEY (service_route_id, direction_id);


--
-- Name: service_routes service_routes_pkey; Type: CONSTRAINT; Schema: route_raw; Owner: -
--

ALTER TABLE ONLY route_raw.service_routes
    ADD CONSTRAINT service_routes_pkey PRIMARY KEY (service_route_id);


--
-- Name: coverage_gaps coverage_gaps_dedupe_key_key; Type: CONSTRAINT; Schema: route_review; Owner: -
--

ALTER TABLE ONLY route_review.coverage_gaps
    ADD CONSTRAINT coverage_gaps_dedupe_key_key UNIQUE (dedupe_key);


--
-- Name: coverage_gaps coverage_gaps_pkey; Type: CONSTRAINT; Schema: route_review; Owner: -
--

ALTER TABLE ONLY route_review.coverage_gaps
    ADD CONSTRAINT coverage_gaps_pkey PRIMARY KEY (gap_id);


--
-- Name: route_job_dedupe_groups route_job_dedupe_groups_pkey; Type: CONSTRAINT; Schema: route_review; Owner: -
--

ALTER TABLE ONLY route_review.route_job_dedupe_groups
    ADD CONSTRAINT route_job_dedupe_groups_pkey PRIMARY KEY (dedupe_group_id);


--
-- Name: route_job_dedupe_memberships route_job_dedupe_memberships_pkey; Type: CONSTRAINT; Schema: route_review; Owner: -
--

ALTER TABLE ONLY route_review.route_job_dedupe_memberships
    ADD CONSTRAINT route_job_dedupe_memberships_pkey PRIMARY KEY (route_id);


--
-- Name: delete_events delete_events_pkey; Type: CONSTRAINT; Schema: route_trash; Owner: -
--

ALTER TABLE ONLY route_trash.delete_events
    ADD CONSTRAINT delete_events_pkey PRIMARY KEY (delete_event_id);


--
-- Name: trash_items trash_items_pkey; Type: CONSTRAINT; Schema: route_trash; Owner: -
--

ALTER TABLE ONLY route_trash.trash_items
    ADD CONSTRAINT trash_items_pkey PRIMARY KEY (trash_id);


--
-- Name: discovery_runs discovery_runs_pkey; Type: CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.discovery_runs
    ADD CONSTRAINT discovery_runs_pkey PRIMARY KEY (run_id);


--
-- Name: geometry_candidate_sets geometry_candidate_sets_pkey; Type: CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.geometry_candidate_sets
    ADD CONSTRAINT geometry_candidate_sets_pkey PRIMARY KEY (set_id);


--
-- Name: geometry_candidates geometry_candidates_pkey; Type: CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.geometry_candidates
    ADD CONSTRAINT geometry_candidates_pkey PRIMARY KEY (geometry_candidate_id);


--
-- Name: geometry_stop_recovery geometry_stop_recovery_pkey; Type: CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.geometry_stop_recovery
    ADD CONSTRAINT geometry_stop_recovery_pkey PRIMARY KEY (geometry_candidate_id);


--
-- Name: inverse_direction_status inverse_direction_status_pkey; Type: CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.inverse_direction_status
    ADD CONSTRAINT inverse_direction_status_pkey PRIMARY KEY (service_route_id, direction_id);


--
-- Name: manual_sequence_drafts manual_sequence_drafts_pkey; Type: CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.manual_sequence_drafts
    ADD CONSTRAINT manual_sequence_drafts_pkey PRIMARY KEY (draft_id);


--
-- Name: manual_sequence_exports manual_sequence_exports_pkey; Type: CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.manual_sequence_exports
    ADD CONSTRAINT manual_sequence_exports_pkey PRIMARY KEY (export_id);


--
-- Name: relation_stop_prior relation_stop_prior_pkey; Type: CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.relation_stop_prior
    ADD CONSTRAINT relation_stop_prior_pkey PRIMARY KEY (route_id, seq);


--
-- Name: route_approvals route_approvals_pkey; Type: CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.route_approvals
    ADD CONSTRAINT route_approvals_pkey PRIMARY KEY (approval_id);


--
-- Name: sequence_approvals sequence_approvals_pkey; Type: CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.sequence_approvals
    ADD CONSTRAINT sequence_approvals_pkey PRIMARY KEY (sequence_approval_id);


--
-- Name: service_route_approvals service_route_approvals_pkey; Type: CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.service_route_approvals
    ADD CONSTRAINT service_route_approvals_pkey PRIMARY KEY (approval_id);


--
-- Name: service_route_approvals service_route_approvals_service_route_id_key; Type: CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.service_route_approvals
    ADD CONSTRAINT service_route_approvals_service_route_id_key UNIQUE (service_route_id);


--
-- Name: stop_sequence_candidate_sets stop_sequence_candidate_sets_pkey; Type: CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.stop_sequence_candidate_sets
    ADD CONSTRAINT stop_sequence_candidate_sets_pkey PRIMARY KEY (set_id);


--
-- Name: stop_sequence_candidates stop_sequence_candidates_pkey; Type: CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.stop_sequence_candidates
    ADD CONSTRAINT stop_sequence_candidates_pkey PRIMARY KEY (candidate_id);


--
-- Name: valhalla_presets valhalla_presets_name_key; Type: CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.valhalla_presets
    ADD CONSTRAINT valhalla_presets_name_key UNIQUE (name);


--
-- Name: valhalla_presets valhalla_presets_pkey; Type: CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.valhalla_presets
    ADD CONSTRAINT valhalla_presets_pkey PRIMARY KEY (preset_id);


--
-- Name: route_evidence_records route_evidence_records_pkey; Type: CONSTRAINT; Schema: semantics; Owner: -
--

ALTER TABLE ONLY semantics.route_evidence_records
    ADD CONSTRAINT route_evidence_records_pkey PRIMARY KEY (record_id);


--
-- Name: route_evidence_records route_evidence_records_source_type_source_id_key; Type: CONSTRAINT; Schema: semantics; Owner: -
--

ALTER TABLE ONLY semantics.route_evidence_records
    ADD CONSTRAINT route_evidence_records_source_type_source_id_key UNIQUE (source_type, source_id);


--
-- Name: route_name_candidates route_name_candidates_pkey; Type: CONSTRAINT; Schema: semantics; Owner: -
--

ALTER TABLE ONLY semantics.route_name_candidates
    ADD CONSTRAINT route_name_candidates_pkey PRIMARY KEY (candidate_id);


--
-- Name: route_name_candidates route_name_candidates_route_id_run_id_rank_pos_key; Type: CONSTRAINT; Schema: semantics; Owner: -
--

ALTER TABLE ONLY semantics.route_name_candidates
    ADD CONSTRAINT route_name_candidates_route_id_run_id_rank_pos_key UNIQUE (route_id, run_id, rank_pos);


--
-- Name: route_name_feedback route_name_feedback_pkey; Type: CONSTRAINT; Schema: semantics; Owner: -
--

ALTER TABLE ONLY semantics.route_name_feedback
    ADD CONSTRAINT route_name_feedback_pkey PRIMARY KEY (feedback_id);


--
-- Name: route_name_feedback route_name_feedback_route_id_candidate_id_reviewer_key; Type: CONSTRAINT; Schema: semantics; Owner: -
--

ALTER TABLE ONLY semantics.route_name_feedback
    ADD CONSTRAINT route_name_feedback_route_id_candidate_id_reviewer_key UNIQUE (route_id, candidate_id, reviewer);


--
-- Name: route_name_seed_runs route_name_seed_runs_pkey; Type: CONSTRAINT; Schema: semantics; Owner: -
--

ALTER TABLE ONLY semantics.route_name_seed_runs
    ADD CONSTRAINT route_name_seed_runs_pkey PRIMARY KEY (seed_run_id);


--
-- Name: idx_ai_bot_model_metrics_created; Type: INDEX; Schema: ai; Owner: -
--

CREATE INDEX idx_ai_bot_model_metrics_created ON ai.ai_bot_model_metrics USING btree (created_at DESC);


--
-- Name: idx_ai_bot_model_metrics_task_created; Type: INDEX; Schema: ai; Owner: -
--

CREATE INDEX idx_ai_bot_model_metrics_task_created ON ai.ai_bot_model_metrics USING btree (task, created_at DESC);


--
-- Name: idx_ai_bot_run_logs_created_at; Type: INDEX; Schema: ai; Owner: -
--

CREATE INDEX idx_ai_bot_run_logs_created_at ON ai.ai_bot_run_logs USING btree (created_at DESC);


--
-- Name: idx_ai_bot_run_logs_event; Type: INDEX; Schema: ai; Owner: -
--

CREATE INDEX idx_ai_bot_run_logs_event ON ai.ai_bot_run_logs USING btree (event_type, created_at DESC);


--
-- Name: idx_ai_bot_run_logs_node_set; Type: INDEX; Schema: ai; Owner: -
--

CREATE INDEX idx_ai_bot_run_logs_node_set ON ai.ai_bot_run_logs USING btree (node_set_id, created_at DESC);


--
-- Name: idx_ai_bot_run_logs_phase_stage; Type: INDEX; Schema: ai; Owner: -
--

CREATE INDEX idx_ai_bot_run_logs_phase_stage ON ai.ai_bot_run_logs USING btree (phase, stage, created_at DESC);


--
-- Name: idx_ai_bot_run_logs_route; Type: INDEX; Schema: ai; Owner: -
--

CREATE INDEX idx_ai_bot_run_logs_route ON ai.ai_bot_run_logs USING btree (route_id, created_at DESC);


--
-- Name: idx_ai_bot_train_events_created; Type: INDEX; Schema: ai; Owner: -
--

CREATE INDEX idx_ai_bot_train_events_created ON ai.ai_bot_train_events USING btree (created_at DESC);


--
-- Name: idx_ai_bot_train_events_task_created; Type: INDEX; Schema: ai; Owner: -
--

CREATE INDEX idx_ai_bot_train_events_task_created ON ai.ai_bot_train_events USING btree (task, created_at DESC);


--
-- Name: idx_ai_escalations_created; Type: INDEX; Schema: ai; Owner: -
--

CREATE INDEX idx_ai_escalations_created ON ai.ai_escalations USING btree (created_at DESC);


--
-- Name: idx_ai_escalations_status_created; Type: INDEX; Schema: ai; Owner: -
--

CREATE INDEX idx_ai_escalations_status_created ON ai.ai_escalations USING btree (status, created_at DESC);


--
-- Name: idx_ai_label_events_created_at; Type: INDEX; Schema: ai; Owner: -
--

CREATE INDEX idx_ai_label_events_created_at ON ai.ai_label_events USING btree (created_at DESC);


--
-- Name: idx_ai_label_events_entity; Type: INDEX; Schema: ai; Owner: -
--

CREATE INDEX idx_ai_label_events_entity ON ai.ai_label_events USING btree (entity_type, entity_id, created_at DESC);


--
-- Name: idx_ai_label_events_suggestion; Type: INDEX; Schema: ai; Owner: -
--

CREATE INDEX idx_ai_label_events_suggestion ON ai.ai_label_events USING btree (suggestion_id, created_at DESC);


--
-- Name: idx_ai_model_registry_active_created; Type: INDEX; Schema: ai; Owner: -
--

CREATE INDEX idx_ai_model_registry_active_created ON ai.ai_model_registry USING btree (is_active, created_at DESC);


--
-- Name: idx_ai_model_registry_phase_created; Type: INDEX; Schema: ai; Owner: -
--

CREATE INDEX idx_ai_model_registry_phase_created ON ai.ai_model_registry USING btree (phase, created_at DESC);


--
-- Name: idx_ai_suggestions_created_at; Type: INDEX; Schema: ai; Owner: -
--

CREATE INDEX idx_ai_suggestions_created_at ON ai.ai_suggestions USING btree (created_at DESC);


--
-- Name: idx_ai_suggestions_entity_status; Type: INDEX; Schema: ai; Owner: -
--

CREATE INDEX idx_ai_suggestions_entity_status ON ai.ai_suggestions USING btree (entity_type, entity_id, status);


--
-- Name: idx_ai_suggestions_phase_status; Type: INDEX; Schema: ai; Owner: -
--

CREATE INDEX idx_ai_suggestions_phase_status ON ai.ai_suggestions USING btree (phase, status);


--
-- Name: idx_ai_training_dataset_created_at; Type: INDEX; Schema: ai; Owner: -
--

CREATE INDEX idx_ai_training_dataset_created_at ON ai.ai_training_dataset USING btree (created_at DESC);


--
-- Name: idx_ai_training_dataset_entity; Type: INDEX; Schema: ai; Owner: -
--

CREATE INDEX idx_ai_training_dataset_entity ON ai.ai_training_dataset USING btree (entity_type, entity_id);


--
-- Name: idx_ai_training_dataset_phase_label; Type: INDEX; Schema: ai; Owner: -
--

CREATE INDEX idx_ai_training_dataset_phase_label ON ai.ai_training_dataset USING btree (phase, label, created_at DESC);


--
-- Name: uq_ai_model_registry_active; Type: INDEX; Schema: ai; Owner: -
--

CREATE UNIQUE INDEX uq_ai_model_registry_active ON ai.ai_model_registry USING btree (COALESCE(phase, ''::text), model_name) WHERE (is_active = true);


--
-- Name: uq_ai_model_registry_identity; Type: INDEX; Schema: ai; Owner: -
--

CREATE UNIQUE INDEX uq_ai_model_registry_identity ON ai.ai_model_registry USING btree (COALESCE(phase, ''::text), model_name, model_version);


--
-- Name: idx_diagnostics_route_phase; Type: INDEX; Schema: automation; Owner: -
--

CREATE INDEX idx_diagnostics_route_phase ON automation.diagnostics USING btree (route_id, phase, created_at DESC);


--
-- Name: idx_diagnostics_run_route; Type: INDEX; Schema: automation; Owner: -
--

CREATE INDEX idx_diagnostics_run_route ON automation.diagnostics USING btree (run_id, route_id);


--
-- Name: idx_patches_run_route; Type: INDEX; Schema: automation; Owner: -
--

CREATE INDEX idx_patches_run_route ON automation.patches USING btree (run_id, route_id);


--
-- Name: idx_route_layover_policy_route; Type: INDEX; Schema: catalog; Owner: -
--

CREATE INDEX idx_route_layover_policy_route ON catalog.route_layover_policy USING btree (route_id);


--
-- Name: idx_route_schedule_profile_route; Type: INDEX; Schema: catalog; Owner: -
--

CREATE INDEX idx_route_schedule_profile_route ON catalog.route_schedule_profile USING btree (route_id, direction_id);


--
-- Name: idx_route_service_days_route; Type: INDEX; Schema: catalog; Owner: -
--

CREATE INDEX idx_route_service_days_route ON catalog.route_service_days USING btree (route_id);


--
-- Name: idx_route_service_exceptions_route; Type: INDEX; Schema: catalog; Owner: -
--

CREATE INDEX idx_route_service_exceptions_route ON catalog.route_service_exceptions USING btree (route_id);


--
-- Name: idx_approval_queue_pending; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_approval_queue_pending ON console.approval_queue USING btree (created_at DESC) WHERE (action IS NULL);


--
-- Name: idx_approval_queue_session_id; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_approval_queue_session_id ON console.approval_queue USING btree (session_id);


--
-- Name: idx_approval_queue_step_id; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_approval_queue_step_id ON console.approval_queue USING btree (step_id);


--
-- Name: idx_console_api_keys_revoked; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_console_api_keys_revoked ON console.api_keys USING btree (revoked_at);


--
-- Name: idx_console_api_keys_user; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_console_api_keys_user ON console.api_keys USING btree (user_id);


--
-- Name: idx_console_audit_created_at; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_console_audit_created_at ON console.audit_events USING btree (created_at DESC);


--
-- Name: idx_console_audit_phase; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_console_audit_phase ON console.audit_events USING btree (phase);


--
-- Name: idx_console_audit_user; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_console_audit_user ON console.audit_events USING btree (user_id);


--
-- Name: idx_console_decisions_created_at; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_console_decisions_created_at ON console.phase_decisions USING btree (created_at DESC);


--
-- Name: idx_console_decisions_phase_item; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_console_decisions_phase_item ON console.phase_decisions USING btree (phase, item_id, created_at DESC);


--
-- Name: idx_console_decisions_user; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_console_decisions_user ON console.phase_decisions USING btree (user_id);


--
-- Name: idx_console_email_outbox_created; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_console_email_outbox_created ON console.email_outbox USING btree (created_at DESC);


--
-- Name: idx_console_email_verif_active; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_console_email_verif_active ON console.email_verification_tokens USING btree (expires_at) WHERE (consumed_at IS NULL);


--
-- Name: idx_console_email_verif_user; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_console_email_verif_user ON console.email_verification_tokens USING btree (user_id, created_at DESC);


--
-- Name: idx_console_exports_created_at; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_console_exports_created_at ON console.export_jobs USING btree (created_at DESC);


--
-- Name: idx_console_exports_status; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_console_exports_status ON console.export_jobs USING btree (status);


--
-- Name: idx_console_sessions_expires; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_console_sessions_expires ON console.sessions USING btree (expires_at);


--
-- Name: idx_console_sessions_user; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_console_sessions_user ON console.sessions USING btree (user_id);


--
-- Name: idx_console_sync_events_synced_at; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_console_sync_events_synced_at ON console.sync_events USING btree (synced_at DESC);


--
-- Name: idx_console_users_active; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_console_users_active ON console.users USING btree (is_active);


--
-- Name: idx_console_users_role; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_console_users_role ON console.users USING btree (role);


--
-- Name: idx_orch_sessions_created_at; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_orch_sessions_created_at ON console.orchestrator_sessions USING btree (created_at DESC);


--
-- Name: idx_orch_sessions_status; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_orch_sessions_status ON console.orchestrator_sessions USING btree (status);


--
-- Name: idx_orch_sessions_user_id; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_orch_sessions_user_id ON console.orchestrator_sessions USING btree (user_id);


--
-- Name: idx_orch_steps_session_id; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_orch_steps_session_id ON console.orchestrator_steps USING btree (session_id);


--
-- Name: idx_orch_steps_status; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_orch_steps_status ON console.orchestrator_steps USING btree (status);


--
-- Name: idx_orch_steps_step_key; Type: INDEX; Schema: console; Owner: -
--

CREATE INDEX idx_orch_steps_step_key ON console.orchestrator_steps USING btree (step_key);


--
-- Name: idx_geo_alias_emb_ivfflat_cosine; Type: INDEX; Schema: geo_prod; Owner: -
--

CREATE INDEX idx_geo_alias_emb_ivfflat_cosine ON geo_prod.place_alias_embeddings USING ivfflat (embedding public.vector_cosine_ops) WITH (lists='100');


--
-- Name: idx_geo_alias_emb_place; Type: INDEX; Schema: geo_prod; Owner: -
--

CREATE INDEX idx_geo_alias_emb_place ON geo_prod.place_alias_embeddings USING btree (place_id);


--
-- Name: idx_geo_node_place_map_place; Type: INDEX; Schema: geo_prod; Owner: -
--

CREATE INDEX idx_geo_node_place_map_place ON geo_prod.node_place_map USING btree (place_id);


--
-- Name: idx_geo_place_aliases_norm; Type: INDEX; Schema: geo_prod; Owner: -
--

CREATE INDEX idx_geo_place_aliases_norm ON geo_prod.place_aliases USING btree (normalized_alias);


--
-- Name: idx_geo_place_aliases_norm_fts; Type: INDEX; Schema: geo_prod; Owner: -
--

CREATE INDEX idx_geo_place_aliases_norm_fts ON geo_prod.place_aliases USING gin (to_tsvector('simple'::regconfig, normalized_alias));


--
-- Name: idx_geo_place_aliases_norm_trgm; Type: INDEX; Schema: geo_prod; Owner: -
--

CREATE INDEX idx_geo_place_aliases_norm_trgm ON geo_prod.place_aliases USING gin (normalized_alias public.gin_trgm_ops);


--
-- Name: idx_geo_place_aliases_place; Type: INDEX; Schema: geo_prod; Owner: -
--

CREATE INDEX idx_geo_place_aliases_place ON geo_prod.place_aliases USING btree (place_id);


--
-- Name: idx_geo_place_emb_ivfflat_cosine; Type: INDEX; Schema: geo_prod; Owner: -
--

CREATE INDEX idx_geo_place_emb_ivfflat_cosine ON geo_prod.place_embeddings USING ivfflat (embedding public.vector_cosine_ops) WITH (lists='100');


--
-- Name: idx_geo_places_geom_gist; Type: INDEX; Schema: geo_prod; Owner: -
--

CREATE INDEX idx_geo_places_geom_gist ON geo_prod.places USING gist (geom);


--
-- Name: idx_geo_places_name; Type: INDEX; Schema: geo_prod; Owner: -
--

CREATE INDEX idx_geo_places_name ON geo_prod.places USING btree (canonical_name);


--
-- Name: idx_geo_places_type; Type: INDEX; Schema: geo_prod; Owner: -
--

CREATE INDEX idx_geo_places_type ON geo_prod.places USING btree (place_type);


--
-- Name: idx_place_history_changed_at; Type: INDEX; Schema: geo_prod; Owner: -
--

CREATE INDEX idx_place_history_changed_at ON geo_prod.place_history USING btree (changed_at);


--
-- Name: idx_place_history_place_id; Type: INDEX; Schema: geo_prod; Owner: -
--

CREATE INDEX idx_place_history_place_id ON geo_prod.place_history USING btree (place_id);


--
-- Name: idx_places_province; Type: INDEX; Schema: geo_prod; Owner: -
--

CREATE INDEX idx_places_province ON geo_prod.places USING btree (province);


--
-- Name: idx_geo_extract_runs_context_key; Type: INDEX; Schema: geo_raw; Owner: -
--

CREATE INDEX idx_geo_extract_runs_context_key ON geo_raw.extract_runs USING btree (context_key);


--
-- Name: idx_geo_extract_runs_extracted_at; Type: INDEX; Schema: geo_raw; Owner: -
--

CREATE INDEX idx_geo_extract_runs_extracted_at ON geo_raw.extract_runs USING btree (extracted_at);


--
-- Name: idx_geo_name_evidence_node; Type: INDEX; Schema: geo_raw; Owner: -
--

CREATE INDEX idx_geo_name_evidence_node ON geo_raw.name_evidence USING btree (node_id);


--
-- Name: idx_geo_name_evidence_run; Type: INDEX; Schema: geo_raw; Owner: -
--

CREATE INDEX idx_geo_name_evidence_run ON geo_raw.name_evidence USING btree (extract_run_id);


--
-- Name: idx_geo_name_evidence_source; Type: INDEX; Schema: geo_raw; Owner: -
--

CREATE INDEX idx_geo_name_evidence_source ON geo_raw.name_evidence USING btree (source);


--
-- Name: idx_geo_name_evidence_tags_gin; Type: INDEX; Schema: geo_raw; Owner: -
--

CREATE INDEX idx_geo_name_evidence_tags_gin ON geo_raw.name_evidence USING gin (tags_snapshot);


--
-- Name: idx_geo_alias_candidates_place; Type: INDEX; Schema: geo_work; Owner: -
--

CREATE INDEX idx_geo_alias_candidates_place ON geo_work.alias_candidates USING btree (place_candidate_id);


--
-- Name: idx_geo_name_candidates_rank; Type: INDEX; Schema: geo_work; Owner: -
--

CREATE INDEX idx_geo_name_candidates_rank ON geo_work.place_name_candidates USING btree (place_set_id, place_candidate_id, model_rank, model_score DESC NULLS LAST);


--
-- Name: idx_geo_name_candidates_set; Type: INDEX; Schema: geo_work; Owner: -
--

CREATE INDEX idx_geo_name_candidates_set ON geo_work.place_name_candidates USING btree (place_set_id, place_candidate_id);


--
-- Name: idx_geo_name_feedback_chosen; Type: INDEX; Schema: geo_work; Owner: -
--

CREATE INDEX idx_geo_name_feedback_chosen ON geo_work.place_name_feedback USING btree (chosen_name_candidate_id);


--
-- Name: idx_geo_name_feedback_set; Type: INDEX; Schema: geo_work; Owner: -
--

CREATE INDEX idx_geo_name_feedback_set ON geo_work.place_name_feedback USING btree (place_set_id, place_candidate_id, created_at DESC);


--
-- Name: idx_geo_node_ctx_context; Type: INDEX; Schema: geo_work; Owner: -
--

CREATE INDEX idx_geo_node_ctx_context ON geo_work.node_geo_context USING btree (context_key, extract_run_id);


--
-- Name: idx_geo_node_ctx_geohash; Type: INDEX; Schema: geo_work; Owner: -
--

CREATE INDEX idx_geo_node_ctx_geohash ON geo_work.node_geo_context USING btree (geohash7);


--
-- Name: idx_geo_node_place_map_work_place; Type: INDEX; Schema: geo_work; Owner: -
--

CREATE INDEX idx_geo_node_place_map_work_place ON geo_work.node_place_map_work USING btree (place_set_id, place_candidate_id);


--
-- Name: idx_geo_node_place_map_work_set; Type: INDEX; Schema: geo_work; Owner: -
--

CREATE INDEX idx_geo_node_place_map_work_set ON geo_work.node_place_map_work USING btree (place_set_id);


--
-- Name: idx_geo_node_place_work_place; Type: INDEX; Schema: geo_work; Owner: -
--

CREATE INDEX idx_geo_node_place_work_place ON geo_work.node_place_map_work USING btree (place_set_id, place_candidate_id);


--
-- Name: idx_geo_place_candidates_set; Type: INDEX; Schema: geo_work; Owner: -
--

CREATE INDEX idx_geo_place_candidates_set ON geo_work.place_candidates USING btree (place_set_id);


--
-- Name: idx_geo_place_candidates_type; Type: INDEX; Schema: geo_work; Owner: -
--

CREATE INDEX idx_geo_place_candidates_type ON geo_work.place_candidates USING btree (proposed_place_type);


--
-- Name: idx_geo_place_sets_context_key; Type: INDEX; Schema: geo_work; Owner: -
--

CREATE INDEX idx_geo_place_sets_context_key ON geo_work.place_candidate_sets USING btree (context_key);


--
-- Name: idx_geo_place_sets_created_at; Type: INDEX; Schema: geo_work; Owner: -
--

CREATE INDEX idx_geo_place_sets_created_at ON geo_work.place_candidate_sets USING btree (created_at);


--
-- Name: idx_geo_place_sets_extract_run; Type: INDEX; Schema: geo_work; Owner: -
--

CREATE INDEX idx_geo_place_sets_extract_run ON geo_work.place_candidate_sets USING btree (source_extract_run_id);


--
-- Name: idx_geo_poi_stop_feedback_set; Type: INDEX; Schema: geo_work; Owner: -
--

CREATE INDEX idx_geo_poi_stop_feedback_set ON geo_work.poi_stop_feedback USING btree (place_set_id, place_candidate_id, created_at DESC);


--
-- Name: idx_geo_selection_log_chosen_set; Type: INDEX; Schema: geo_work; Owner: -
--

CREATE INDEX idx_geo_selection_log_chosen_set ON geo_work.selection_log USING btree (chosen_set_id);


--
-- Name: idx_geo_selection_log_selected_at; Type: INDEX; Schema: geo_work; Owner: -
--

CREATE INDEX idx_geo_selection_log_selected_at ON geo_work.selection_log USING btree (selected_at);


--
-- Name: idx_gtfs_artifacts_approval_token; Type: INDEX; Schema: gtfs; Owner: -
--

CREATE INDEX idx_gtfs_artifacts_approval_token ON gtfs.gtfs_artifacts USING btree (approval_token);


--
-- Name: idx_gtfs_artifacts_hash; Type: INDEX; Schema: gtfs; Owner: -
--

CREATE INDEX idx_gtfs_artifacts_hash ON gtfs.gtfs_artifacts USING btree (file_hash);


--
-- Name: idx_gtfs_artifacts_status_created; Type: INDEX; Schema: gtfs; Owner: -
--

CREATE INDEX idx_gtfs_artifacts_status_created ON gtfs.gtfs_artifacts USING btree (status, created_at DESC);


--
-- Name: idx_gtfs_audit_log_action; Type: INDEX; Schema: gtfs; Owner: -
--

CREATE INDEX idx_gtfs_audit_log_action ON gtfs.gtfs_audit_log USING btree (action, created_at DESC);


--
-- Name: idx_gtfs_audit_log_artifact; Type: INDEX; Schema: gtfs; Owner: -
--

CREATE INDEX idx_gtfs_audit_log_artifact ON gtfs.gtfs_audit_log USING btree (artifact_id, created_at DESC);


--
-- Name: idx_gtfs_notifications_artifact; Type: INDEX; Schema: gtfs; Owner: -
--

CREATE INDEX idx_gtfs_notifications_artifact ON gtfs.gtfs_notifications USING btree (artifact_id, created_at DESC);


--
-- Name: idx_gtfs_notifications_status; Type: INDEX; Schema: gtfs; Owner: -
--

CREATE INDEX idx_gtfs_notifications_status ON gtfs.gtfs_notifications USING btree (status, created_at DESC);


--
-- Name: idx_gtfs_prod_feed_versions_current; Type: INDEX; Schema: gtfs_prod; Owner: -
--

CREATE INDEX idx_gtfs_prod_feed_versions_current ON gtfs_prod.feed_versions USING btree (is_current, published_at DESC);


--
-- Name: idx_export_runs_gtfs_id; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_export_runs_gtfs_id ON gtfs_work.export_runs USING btree (gtfs_id);


--
-- Name: idx_gtfs_agency_province; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_gtfs_agency_province ON gtfs_work.gtfs_agency USING btree (province);


--
-- Name: idx_gtfs_builds_created_at; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_gtfs_builds_created_at ON gtfs_work.gtfs_builds USING btree (created_at DESC);


--
-- Name: idx_gtfs_builds_export_run_id; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_gtfs_builds_export_run_id ON gtfs_work.gtfs_builds USING btree (export_run_id);


--
-- Name: idx_gtfs_calendar_dates_export_service_date; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_gtfs_calendar_dates_export_service_date ON gtfs_work.gtfs_calendar_dates USING btree (export_run_id, service_id, date);


--
-- Name: idx_gtfs_calendar_export_service; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_gtfs_calendar_export_service ON gtfs_work.gtfs_calendar USING btree (export_run_id, service_id);


--
-- Name: idx_gtfs_frequencies_export_trip_start; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_gtfs_frequencies_export_trip_start ON gtfs_work.gtfs_frequencies USING btree (export_run_id, trip_id, start_time);


--
-- Name: idx_gtfs_loadings_gtfs_id; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_gtfs_loadings_gtfs_id ON gtfs_work.gtfs_loadings USING btree (gtfs_id, created_at DESC);


--
-- Name: idx_gtfs_routes_export_route; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_gtfs_routes_export_route ON gtfs_work.gtfs_routes USING btree (export_run_id, route_id);


--
-- Name: idx_gtfs_routes_gtfs_id; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_gtfs_routes_gtfs_id ON gtfs_work.gtfs_routes USING btree (gtfs_id, route_id);


--
-- Name: idx_gtfs_shapes_export_shape_seq; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_gtfs_shapes_export_shape_seq ON gtfs_work.gtfs_shapes USING btree (export_run_id, shape_id, shape_pt_sequence);


--
-- Name: idx_gtfs_stop_times_export_stop; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_gtfs_stop_times_export_stop ON gtfs_work.gtfs_stop_times USING btree (export_run_id, stop_id);


--
-- Name: idx_gtfs_stop_times_export_trip_stop; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_gtfs_stop_times_export_trip_stop ON gtfs_work.gtfs_stop_times USING btree (export_run_id, trip_id, stop_sequence);


--
-- Name: idx_gtfs_stop_times_gtfs_id; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_gtfs_stop_times_gtfs_id ON gtfs_work.gtfs_stop_times USING btree (gtfs_id, trip_id, stop_sequence);


--
-- Name: idx_gtfs_stops_export_stop_name; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_gtfs_stops_export_stop_name ON gtfs_work.gtfs_stops USING btree (export_run_id, stop_name, stop_id);


--
-- Name: idx_gtfs_trips_export_route_shape; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_gtfs_trips_export_route_shape ON gtfs_work.gtfs_trips USING btree (export_run_id, route_id, shape_id) WHERE (shape_id IS NOT NULL);


--
-- Name: idx_gtfs_trips_export_route_trip; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_gtfs_trips_export_route_trip ON gtfs_work.gtfs_trips USING btree (export_run_id, route_id, trip_id);


--
-- Name: idx_gtfs_trips_export_service; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_gtfs_trips_export_service ON gtfs_work.gtfs_trips USING btree (export_run_id, service_id);


--
-- Name: idx_gtfs_trips_gtfs_id; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_gtfs_trips_gtfs_id ON gtfs_work.gtfs_trips USING btree (gtfs_id, route_id, direction_id);


--
-- Name: idx_gtfs_work_agency_catalog_norm; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE UNIQUE INDEX idx_gtfs_work_agency_catalog_norm ON gtfs_work.agency_catalog USING btree (agency_name_norm);


--
-- Name: idx_gtfs_work_agency_match_requests_route_pending; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE UNIQUE INDEX idx_gtfs_work_agency_match_requests_route_pending ON gtfs_work.agency_match_requests USING btree (route_id) WHERE (status = 'pending'::text);


--
-- Name: idx_gtfs_work_export_runs_created; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_gtfs_work_export_runs_created ON gtfs_work.export_runs USING btree (created_at DESC);


--
-- Name: idx_gtfs_work_revision_events_run_time; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_gtfs_work_revision_events_run_time ON gtfs_work.revision_events USING btree (export_run_id, edited_at DESC);


--
-- Name: idx_gtfs_work_route_agency_links_agency; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_gtfs_work_route_agency_links_agency ON gtfs_work.route_agency_links USING btree (agency_id);


--
-- Name: idx_gtfs_work_route_schedule_profiles_route; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_gtfs_work_route_schedule_profiles_route ON gtfs_work.route_schedule_profiles USING btree (route_id, direction_id, is_active);


--
-- Name: idx_gtfs_work_service_windows_profile; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_gtfs_work_service_windows_profile ON gtfs_work.service_windows USING btree (profile_id, is_active);


--
-- Name: idx_gtfs_work_upload_runs_uploaded_at; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_gtfs_work_upload_runs_uploaded_at ON gtfs_work.upload_runs USING btree (uploaded_at DESC);


--
-- Name: idx_revision_events_gtfs_id; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_revision_events_gtfs_id ON gtfs_work.revision_events USING btree (gtfs_id, edited_at DESC);


--
-- Name: idx_route_runtime_estimate_bindings_estimate; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_route_runtime_estimate_bindings_estimate ON gtfs_work.route_runtime_estimate_bindings USING btree (estimate_id);


--
-- Name: idx_rre_status; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_rre_status ON gtfs_work.runtime_route_estimates USING btree (status);


--
-- Name: idx_runtime_catalog_items_catalog; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_runtime_catalog_items_catalog ON gtfs_work.runtime_catalog_items USING btree (catalog_key, is_active, item_code);


--
-- Name: idx_runtime_route_estimates_route; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_runtime_route_estimates_route ON gtfs_work.runtime_route_estimates USING btree (route_id, direction_id, estimated_at DESC);


--
-- Name: idx_runtime_route_leg_features_est; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_runtime_route_leg_features_est ON gtfs_work.runtime_route_leg_features USING btree (estimate_id, leg_idx);


--
-- Name: idx_trip_departures_export; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_trip_departures_export ON gtfs_work.trip_departures USING btree (export_run_id);


--
-- Name: idx_upload_runs_gtfs_id; Type: INDEX; Schema: gtfs_work; Owner: -
--

CREATE INDEX idx_upload_runs_gtfs_id ON gtfs_work.upload_runs USING btree (gtfs_id);


--
-- Name: idx_dca_route; Type: INDEX; Schema: node_prod; Owner: -
--

CREATE INDEX idx_dca_route ON node_prod.direction_construction_audit USING btree (route_id);


--
-- Name: idx_dca_run; Type: INDEX; Schema: node_prod; Owner: -
--

CREATE INDEX idx_dca_run ON node_prod.direction_construction_audit USING btree (run_id);


--
-- Name: idx_dca_state; Type: INDEX; Schema: node_prod; Owner: -
--

CREATE INDEX idx_dca_state ON node_prod.direction_construction_audit USING btree (new_state);


--
-- Name: idx_node_prod_nodes_geom_gist; Type: INDEX; Schema: node_prod; Owner: -
--

CREATE INDEX idx_node_prod_nodes_geom_gist ON node_prod.nodes USING gist (geom);


--
-- Name: idx_node_prod_nodes_name; Type: INDEX; Schema: node_prod; Owner: -
--

CREATE INDEX idx_node_prod_nodes_name ON node_prod.nodes USING btree (name);


--
-- Name: idx_node_prod_nodes_tags_gin; Type: INDEX; Schema: node_prod; Owner: -
--

CREATE INDEX idx_node_prod_nodes_tags_gin ON node_prod.nodes USING gin (chosen_tags);


--
-- Name: idx_node_prod_nodes_type; Type: INDEX; Schema: node_prod; Owner: -
--

CREATE INDEX idx_node_prod_nodes_type ON node_prod.nodes USING btree (node_type);


--
-- Name: idx_nodes_osm_id_notnull; Type: INDEX; Schema: node_prod; Owner: -
--

CREATE INDEX idx_nodes_osm_id_notnull ON node_prod.nodes USING btree (osm_id) WHERE (osm_id IS NOT NULL);


--
-- Name: idx_nodes_province; Type: INDEX; Schema: node_prod; Owner: -
--

CREATE INDEX idx_nodes_province ON node_prod.nodes USING btree (province);


--
-- Name: idx_nodes_synthetic_by_coords; Type: INDEX; Schema: node_prod; Owner: -
--

CREATE INDEX idx_nodes_synthetic_by_coords ON node_prod.nodes USING gist (geom) WHERE (source_type = ANY (ARRAY['poi_anchored_path_projected'::text, 'path_corridor_projected'::text, 'path_intersection'::text, 'research_coords_path_snapped'::text, 'pure_synthesis'::text]));


--
-- Name: idx_nodes_synthetic_pending; Type: INDEX; Schema: node_prod; Owner: -
--

CREATE INDEX idx_nodes_synthetic_pending ON node_prod.nodes USING btree (source_type, synthetic_review_state) WHERE ((source_type = ANY (ARRAY['poi_anchored_path_projected'::text, 'path_corridor_projected'::text, 'path_intersection'::text, 'research_coords_path_snapped'::text, 'pure_synthesis'::text])) AND (synthetic_review_state = 'pending'::text));


--
-- Name: idx_psa_node; Type: INDEX; Schema: node_prod; Owner: -
--

CREATE INDEX idx_psa_node ON node_prod.precision_snap_audit USING btree (node_id);


--
-- Name: idx_psa_route; Type: INDEX; Schema: node_prod; Owner: -
--

CREATE INDEX idx_psa_route ON node_prod.precision_snap_audit USING btree (route_id);


--
-- Name: idx_psa_run; Type: INDEX; Schema: node_prod; Owner: -
--

CREATE INDEX idx_psa_run ON node_prod.precision_snap_audit USING btree (run_id);


--
-- Name: idx_synthesis_events_unit_stage; Type: INDEX; Schema: node_prod; Owner: -
--

CREATE INDEX idx_synthesis_events_unit_stage ON node_prod.synthesis_events USING btree (unit, stage, created_at DESC);


--
-- Name: ix_stop_treatment_log_caller; Type: INDEX; Schema: node_prod; Owner: -
--

CREATE INDEX ix_stop_treatment_log_caller ON node_prod.stop_treatment_log USING btree (caller, treated_at DESC);


--
-- Name: ix_stop_treatment_log_failures; Type: INDEX; Schema: node_prod; Owner: -
--

CREATE INDEX ix_stop_treatment_log_failures ON node_prod.stop_treatment_log USING btree (treated_at DESC) WHERE (success = false);


--
-- Name: ix_stop_treatment_log_node; Type: INDEX; Schema: node_prod; Owner: -
--

CREATE INDEX ix_stop_treatment_log_node ON node_prod.stop_treatment_log USING btree (node_id, treated_at DESC);


--
-- Name: ix_stop_treatment_log_operation; Type: INDEX; Schema: node_prod; Owner: -
--

CREATE INDEX ix_stop_treatment_log_operation ON node_prod.stop_treatment_log USING btree (operation, success);


--
-- Name: idx_node_overpass_elements_geom_gist; Type: INDEX; Schema: node_raw; Owner: -
--

CREATE INDEX idx_node_overpass_elements_geom_gist ON node_raw.overpass_elements USING gist (geom);


--
-- Name: idx_node_overpass_elements_osm; Type: INDEX; Schema: node_raw; Owner: -
--

CREATE INDEX idx_node_overpass_elements_osm ON node_raw.overpass_elements USING btree (osm_type, osm_id);


--
-- Name: idx_node_overpass_elements_run_id; Type: INDEX; Schema: node_raw; Owner: -
--

CREATE INDEX idx_node_overpass_elements_run_id ON node_raw.overpass_elements USING btree (run_id);


--
-- Name: idx_node_overpass_elements_tags_gin; Type: INDEX; Schema: node_raw; Owner: -
--

CREATE INDEX idx_node_overpass_elements_tags_gin ON node_raw.overpass_elements USING gin (tags);


--
-- Name: idx_node_overpass_queries_action_id; Type: INDEX; Schema: node_raw; Owner: -
--

CREATE INDEX idx_node_overpass_queries_action_id ON node_raw.overpass_queries USING btree (action_id);


--
-- Name: idx_node_overpass_queries_params_gin; Type: INDEX; Schema: node_raw; Owner: -
--

CREATE INDEX idx_node_overpass_queries_params_gin ON node_raw.overpass_queries USING gin (params);


--
-- Name: idx_node_overpass_runs_fetched_at; Type: INDEX; Schema: node_raw; Owner: -
--

CREATE INDEX idx_node_overpass_runs_fetched_at ON node_raw.overpass_runs USING btree (fetched_at);


--
-- Name: idx_node_overpass_runs_query_id; Type: INDEX; Schema: node_raw; Owner: -
--

CREATE INDEX idx_node_overpass_runs_query_id ON node_raw.overpass_runs USING btree (query_id);


--
-- Name: idx_mv_road_lines_geom; Type: INDEX; Schema: node_work; Owner: -
--

CREATE INDEX idx_mv_road_lines_geom ON node_work.mv_road_lines USING gist (geom);


--
-- Name: idx_node_candidates_geom_gist; Type: INDEX; Schema: node_work; Owner: -
--

CREATE INDEX idx_node_candidates_geom_gist ON node_work.node_candidates USING gist (geom);


--
-- Name: idx_node_candidates_run; Type: INDEX; Schema: node_work; Owner: -
--

CREATE INDEX idx_node_candidates_run ON node_work.node_candidates USING btree (source_run_id);


--
-- Name: idx_node_candidates_set; Type: INDEX; Schema: node_work; Owner: -
--

CREATE INDEX idx_node_candidates_set ON node_work.node_candidates USING btree (node_set_id);


--
-- Name: idx_node_candidates_tag_kind; Type: INDEX; Schema: node_work; Owner: -
--

CREATE INDEX idx_node_candidates_tag_kind ON node_work.node_candidates USING btree (tag_kind);


--
-- Name: idx_node_candidates_tags_gin; Type: INDEX; Schema: node_work; Owner: -
--

CREATE INDEX idx_node_candidates_tags_gin ON node_work.node_candidates USING gin (tags);


--
-- Name: idx_node_clusters_cluster; Type: INDEX; Schema: node_work; Owner: -
--

CREATE INDEX idx_node_clusters_cluster ON node_work.node_clusters USING btree (node_set_id, cluster_id);


--
-- Name: idx_node_features_confidence; Type: INDEX; Schema: node_work; Owner: -
--

CREATE INDEX idx_node_features_confidence ON node_work.node_features USING btree (confidence_v0);


--
-- Name: idx_node_review_requests_created; Type: INDEX; Schema: node_work; Owner: -
--

CREATE INDEX idx_node_review_requests_created ON node_work.node_review_requests USING btree (created_at DESC);


--
-- Name: idx_node_review_requests_route_seq; Type: INDEX; Schema: node_work; Owner: -
--

CREATE INDEX idx_node_review_requests_route_seq ON node_work.node_review_requests USING btree (route_id, seq);


--
-- Name: idx_node_review_requests_source_status; Type: INDEX; Schema: node_work; Owner: -
--

CREATE INDEX idx_node_review_requests_source_status ON node_work.node_review_requests USING btree (source, status, created_at DESC);


--
-- Name: idx_node_sets_created_at; Type: INDEX; Schema: node_work; Owner: -
--

CREATE INDEX idx_node_sets_created_at ON node_work.node_candidate_sets USING btree (created_at);


--
-- Name: idx_nodes_resolved_geom_gist; Type: INDEX; Schema: node_work; Owner: -
--

CREATE INDEX idx_nodes_resolved_geom_gist ON node_work.nodes_resolved USING gist (geom);


--
-- Name: idx_nodes_resolved_set; Type: INDEX; Schema: node_work; Owner: -
--

CREATE INDEX idx_nodes_resolved_set ON node_work.nodes_resolved USING btree (node_set_id);


--
-- Name: idx_route_prod_canonical_sequence_ready; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX idx_route_prod_canonical_sequence_ready ON route_prod.routes USING btree (canonical_sequence_ready);


--
-- Name: idx_route_prod_chosen_sequence; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX idx_route_prod_chosen_sequence ON route_prod.routes USING btree (chosen_stop_sequence_candidate_id);


--
-- Name: idx_route_prod_geom_gist; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX idx_route_prod_geom_gist ON route_prod.routes USING gist (geom);


--
-- Name: idx_route_prod_human_verified; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX idx_route_prod_human_verified ON route_prod.routes USING btree (human_verified);


--
-- Name: idx_route_prod_id_version_deploy; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX idx_route_prod_id_version_deploy ON route_prod.routes USING btree (route_id, version, deploy_status);


--
-- Name: idx_route_prod_naming_confidence; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX idx_route_prod_naming_confidence ON route_prod.routes USING btree (naming_confidence DESC);


--
-- Name: idx_route_prod_service_direction; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX idx_route_prod_service_direction ON route_prod.routes USING btree (service_route_id, direction_id);


--
-- Name: idx_route_semantics_confidence; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX idx_route_semantics_confidence ON route_prod.route_semantics USING btree (naming_confidence DESC);


--
-- Name: idx_route_semantics_search_tsv_gin; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX idx_route_semantics_search_tsv_gin ON route_prod.route_semantics USING gin (search_tsv);


--
-- Name: idx_route_semantics_service_direction; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX idx_route_semantics_service_direction ON route_prod.route_semantics USING btree (service_route_id, direction_id);


--
-- Name: idx_route_semantics_verified; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX idx_route_semantics_verified ON route_prod.route_semantics USING btree (human_verified);


--
-- Name: idx_routes_audit_action_reject; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX idx_routes_audit_action_reject ON route_prod.routes_audit USING btree (action, event_at DESC) WHERE (action = 'REJECT'::text);


--
-- Name: idx_routes_audit_route_id; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX idx_routes_audit_route_id ON route_prod.routes_audit USING btree (route_id, event_at DESC);


--
-- Name: idx_routes_audit_user; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX idx_routes_audit_user ON route_prod.routes_audit USING btree ("session_user", event_at DESC);


--
-- Name: idx_routes_cleanliness; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX idx_routes_cleanliness ON route_prod.routes USING btree (province, cleanliness_status, dirty_reason) WHERE (cleanliness_status IS NOT NULL);


--
-- Name: idx_routes_province; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX idx_routes_province ON route_prod.routes USING btree (province);


--
-- Name: ix_approval_queue_class; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX ix_approval_queue_class ON route_prod.approval_queue USING btree (quality_class) WHERE (status = 'pending'::text);


--
-- Name: ix_approval_queue_cleanup_needed; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX ix_approval_queue_cleanup_needed ON route_prod.approval_queue USING btree (pre_ship_cleanup_applied, quality_class, status) WHERE ((status = 'pending'::text) AND (pre_ship_cleanup_applied = false) AND (quality_class = ANY (ARRAY['good'::text, 'acceptable'::text, 'ship_pending_dr'::text])));


--
-- Name: ix_approval_queue_enqueued; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX ix_approval_queue_enqueued ON route_prod.approval_queue USING btree (enqueued_at DESC);


--
-- Name: ix_approval_queue_pending_dr; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX ix_approval_queue_pending_dr ON route_prod.approval_queue USING gin (pending_dr_batches) WHERE ((status = 'pending'::text) AND (array_length(pending_dr_batches, 1) > 0));


--
-- Name: ix_approval_queue_refill_pending; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX ix_approval_queue_refill_pending ON route_prod.approval_queue USING btree (refill_applied, quality_class, status) WHERE ((status = 'pending'::text) AND (refill_applied = false) AND (quality_class = ANY (ARRAY['good'::text, 'acceptable'::text, 'ship_pending_dr'::text])));


--
-- Name: ix_approval_queue_route_version; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX ix_approval_queue_route_version ON route_prod.approval_queue USING btree (route_code, version);


--
-- Name: ix_approval_queue_status; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX ix_approval_queue_status ON route_prod.approval_queue USING btree (status);


--
-- Name: ix_cleanup_override_audit_queue; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX ix_cleanup_override_audit_queue ON route_prod.cleanup_override_audit USING btree (queue_id, override_at DESC);


--
-- Name: ix_cleanup_resets_queue; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX ix_cleanup_resets_queue ON route_prod.cleanup_v2_to_postapproval_resets USING btree (queue_id, reset_at DESC);


--
-- Name: ix_dr_batch_deps_batch; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX ix_dr_batch_deps_batch ON route_prod.dr_batch_dependencies USING btree (dr_batch_id);


--
-- Name: ix_dr_batch_deps_route; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX ix_dr_batch_deps_route ON route_prod.dr_batch_dependencies USING btree (route_id);


--
-- Name: ix_dr_batch_deps_waiting; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX ix_dr_batch_deps_waiting ON route_prod.dr_batch_dependencies USING btree (status) WHERE (status = 'waiting'::text);


--
-- Name: ix_fix_reports_applied_at; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX ix_fix_reports_applied_at ON route_prod.fix_reports USING btree (applied_at DESC);


--
-- Name: ix_fix_reports_route; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX ix_fix_reports_route ON route_prod.fix_reports USING btree (route_id);


--
-- Name: ix_re_entry_queue_active_unique; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE UNIQUE INDEX ix_re_entry_queue_active_unique ON route_prod.re_entry_queue USING btree (route_id, current_version) WHERE (status = ANY (ARRAY['pending'::text, 'in_progress'::text, 'v2_ready'::text]));


--
-- Name: ix_re_entry_queue_route; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX ix_re_entry_queue_route ON route_prod.re_entry_queue USING btree (route_id);


--
-- Name: ix_re_entry_queue_status_priority; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX ix_re_entry_queue_status_priority ON route_prod.re_entry_queue USING btree (status, priority, scheduled_at);


--
-- Name: ix_refill_audit_queue; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX ix_refill_audit_queue ON route_prod.refill_audit USING btree (queue_id, decided_at DESC);


--
-- Name: ix_refill_audit_stop; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX ix_refill_audit_stop ON route_prod.refill_audit USING btree (candidate_stop_id);


--
-- Name: routes_enrichment_queue_idx; Type: INDEX; Schema: route_prod; Owner: -
--

CREATE INDEX routes_enrichment_queue_idx ON route_prod.routes USING btree (enrichment_score, last_enriched_at NULLS FIRST) WHERE (deploy_status = 'active'::text);


--
-- Name: idx_osm_relations_raw_osm_relation_id; Type: INDEX; Schema: route_raw; Owner: -
--

CREATE INDEX idx_osm_relations_raw_osm_relation_id ON route_raw.osm_relations_raw USING btree (osm_relation_id);


--
-- Name: idx_osm_relations_raw_route_id; Type: INDEX; Schema: route_raw; Owner: -
--

CREATE INDEX idx_osm_relations_raw_route_id ON route_raw.osm_relations_raw USING btree (route_id);


--
-- Name: idx_relation_candidates_relation; Type: INDEX; Schema: route_raw; Owner: -
--

CREATE INDEX idx_relation_candidates_relation ON route_raw.relation_candidates USING btree (osm_relation_id);


--
-- Name: idx_relation_candidates_route; Type: INDEX; Schema: route_raw; Owner: -
--

CREATE INDEX idx_relation_candidates_route ON route_raw.relation_candidates USING btree (route_id, found_at DESC);


--
-- Name: idx_route_jobs_area_ref; Type: INDEX; Schema: route_raw; Owner: -
--

CREATE INDEX idx_route_jobs_area_ref ON route_raw.route_jobs USING btree (area_key, known_ref);


--
-- Name: idx_route_jobs_chosen_relation_created; Type: INDEX; Schema: route_raw; Owner: -
--

CREATE INDEX idx_route_jobs_chosen_relation_created ON route_raw.route_jobs USING btree (chosen_osm_relation_id, created_at DESC) WHERE (chosen_osm_relation_id IS NOT NULL);


--
-- Name: idx_route_jobs_extractor_source_created; Type: INDEX; Schema: route_raw; Owner: -
--

CREATE INDEX idx_route_jobs_extractor_source_created ON route_raw.route_jobs USING btree (extractor_source, created_at DESC);


--
-- Name: idx_route_jobs_is_trashed; Type: INDEX; Schema: route_raw; Owner: -
--

CREATE INDEX idx_route_jobs_is_trashed ON route_raw.route_jobs USING btree (is_trashed) WHERE (is_trashed = true);


--
-- Name: idx_route_jobs_province; Type: INDEX; Schema: route_raw; Owner: -
--

CREATE INDEX idx_route_jobs_province ON route_raw.route_jobs USING btree (province);


--
-- Name: idx_route_jobs_service_direction; Type: INDEX; Schema: route_raw; Owner: -
--

CREATE INDEX idx_route_jobs_service_direction ON route_raw.route_jobs USING btree (service_route_id, direction_id, created_at DESC);


--
-- Name: idx_service_route_directions_service_route; Type: INDEX; Schema: route_raw; Owner: -
--

CREATE INDEX idx_service_route_directions_service_route ON route_raw.service_route_directions USING btree (service_route_id, direction_id);


--
-- Name: uq_relation_candidates_route_relation; Type: INDEX; Schema: route_raw; Owner: -
--

CREATE UNIQUE INDEX uq_relation_candidates_route_relation ON route_raw.relation_candidates USING btree (route_id, osm_relation_id);


--
-- Name: uq_service_route_direction_route_id; Type: INDEX; Schema: route_raw; Owner: -
--

CREATE UNIQUE INDEX uq_service_route_direction_route_id ON route_raw.service_route_directions USING btree (route_id) WHERE (route_id IS NOT NULL);


--
-- Name: idx_coverage_gaps_classification; Type: INDEX; Schema: route_review; Owner: -
--

CREATE INDEX idx_coverage_gaps_classification ON route_review.coverage_gaps USING btree (classification_status, resolution_status, updated_at DESC);


--
-- Name: idx_coverage_gaps_sector; Type: INDEX; Schema: route_review; Owner: -
--

CREATE INDEX idx_coverage_gaps_sector ON route_review.coverage_gaps USING btree (sector_key, resolution_status, manual_priority, updated_at DESC);


--
-- Name: idx_route_job_dedupe_groups_canonical; Type: INDEX; Schema: route_review; Owner: -
--

CREATE INDEX idx_route_job_dedupe_groups_canonical ON route_review.route_job_dedupe_groups USING btree (canonical_route_id, updated_at DESC);


--
-- Name: idx_route_job_dedupe_groups_relation; Type: INDEX; Schema: route_review; Owner: -
--

CREATE INDEX idx_route_job_dedupe_groups_relation ON route_review.route_job_dedupe_groups USING btree (chosen_osm_relation_id, updated_at DESC);


--
-- Name: idx_route_job_dedupe_memberships_canonical; Type: INDEX; Schema: route_review; Owner: -
--

CREATE INDEX idx_route_job_dedupe_memberships_canonical ON route_review.route_job_dedupe_memberships USING btree (canonical_route_id, membership_status);


--
-- Name: idx_route_job_dedupe_memberships_group; Type: INDEX; Schema: route_review; Owner: -
--

CREATE INDEX idx_route_job_dedupe_memberships_group ON route_review.route_job_dedupe_memberships USING btree (dedupe_group_id, membership_role, membership_status);


--
-- Name: idx_delete_events_action_type; Type: INDEX; Schema: route_trash; Owner: -
--

CREATE INDEX idx_delete_events_action_type ON route_trash.delete_events USING btree (action_type);


--
-- Name: idx_delete_events_event_at; Type: INDEX; Schema: route_trash; Owner: -
--

CREATE INDEX idx_delete_events_event_at ON route_trash.delete_events USING btree (event_at DESC);


--
-- Name: idx_delete_events_route_id; Type: INDEX; Schema: route_trash; Owner: -
--

CREATE INDEX idx_delete_events_route_id ON route_trash.delete_events USING btree (route_id);


--
-- Name: idx_delete_events_trash_id; Type: INDEX; Schema: route_trash; Owner: -
--

CREATE INDEX idx_delete_events_trash_id ON route_trash.delete_events USING btree (trash_id);


--
-- Name: idx_trash_items_deleted_at; Type: INDEX; Schema: route_trash; Owner: -
--

CREATE INDEX idx_trash_items_deleted_at ON route_trash.trash_items USING btree (deleted_at DESC);


--
-- Name: idx_trash_items_restore_status; Type: INDEX; Schema: route_trash; Owner: -
--

CREATE INDEX idx_trash_items_restore_status ON route_trash.trash_items USING btree (restore_status);


--
-- Name: idx_trash_items_route_id; Type: INDEX; Schema: route_trash; Owner: -
--

CREATE INDEX idx_trash_items_route_id ON route_trash.trash_items USING btree (route_id);


--
-- Name: idx_trash_items_workflow; Type: INDEX; Schema: route_trash; Owner: -
--

CREATE INDEX idx_trash_items_workflow ON route_trash.trash_items USING btree (deletion_source_workflow);


--
-- Name: idx_discovery_runs_catalog; Type: INDEX; Schema: route_work; Owner: -
--

CREATE INDEX idx_discovery_runs_catalog ON route_work.discovery_runs USING btree (source_catalog, catalog_index);


--
-- Name: idx_discovery_runs_idemp; Type: INDEX; Schema: route_work; Owner: -
--

CREATE UNIQUE INDEX idx_discovery_runs_idemp ON route_work.discovery_runs USING btree (idempotency_key);


--
-- Name: idx_discovery_runs_province; Type: INDEX; Schema: route_work; Owner: -
--

CREATE INDEX idx_discovery_runs_province ON route_work.discovery_runs USING btree (province);


--
-- Name: idx_discovery_runs_status; Type: INDEX; Schema: route_work; Owner: -
--

CREATE INDEX idx_discovery_runs_status ON route_work.discovery_runs USING btree (status, review_status, created_at DESC);


--
-- Name: idx_geometry_candidate_sets_route; Type: INDEX; Schema: route_work; Owner: -
--

CREATE INDEX idx_geometry_candidate_sets_route ON route_work.geometry_candidate_sets USING btree (route_id, created_at DESC);


--
-- Name: idx_geometry_candidates_geom_gist; Type: INDEX; Schema: route_work; Owner: -
--

CREATE INDEX idx_geometry_candidates_geom_gist ON route_work.geometry_candidates USING gist (geom);


--
-- Name: idx_geometry_candidates_seq; Type: INDEX; Schema: route_work; Owner: -
--

CREATE INDEX idx_geometry_candidates_seq ON route_work.geometry_candidates USING btree (stop_sequence_candidate_id);


--
-- Name: idx_geometry_candidates_set_score; Type: INDEX; Schema: route_work; Owner: -
--

CREATE INDEX idx_geometry_candidates_set_score ON route_work.geometry_candidates USING btree (set_id, score DESC);


--
-- Name: idx_geometry_stop_recovery_route; Type: INDEX; Schema: route_work; Owner: -
--

CREATE INDEX idx_geometry_stop_recovery_route ON route_work.geometry_stop_recovery USING btree (route_id, updated_at DESC);


--
-- Name: idx_geometry_stop_recovery_seq; Type: INDEX; Schema: route_work; Owner: -
--

CREATE INDEX idx_geometry_stop_recovery_seq ON route_work.geometry_stop_recovery USING btree (stop_sequence_candidate_id);


--
-- Name: idx_geometry_stop_recovery_set; Type: INDEX; Schema: route_work; Owner: -
--

CREATE INDEX idx_geometry_stop_recovery_set ON route_work.geometry_stop_recovery USING btree (set_id, updated_at DESC);


--
-- Name: idx_inverse_direction_status_bound_route; Type: INDEX; Schema: route_work; Owner: -
--

CREATE INDEX idx_inverse_direction_status_bound_route ON route_work.inverse_direction_status USING btree (bound_route_id) WHERE (bound_route_id IS NOT NULL);


--
-- Name: idx_inverse_direction_status_ready; Type: INDEX; Schema: route_work; Owner: -
--

CREATE INDEX idx_inverse_direction_status_ready ON route_work.inverse_direction_status USING btree (direction_ready, updated_at DESC);


--
-- Name: idx_inverse_direction_status_top_candidate; Type: INDEX; Schema: route_work; Owner: -
--

CREATE INDEX idx_inverse_direction_status_top_candidate ON route_work.inverse_direction_status USING btree (top_candidate_route_id) WHERE (top_candidate_route_id IS NOT NULL);


--
-- Name: idx_manual_sequence_drafts_route; Type: INDEX; Schema: route_work; Owner: -
--

CREATE INDEX idx_manual_sequence_drafts_route ON route_work.manual_sequence_drafts USING btree (route_id, updated_at DESC);


--
-- Name: idx_manual_sequence_exports_gap; Type: INDEX; Schema: route_work; Owner: -
--

CREATE INDEX idx_manual_sequence_exports_gap ON route_work.manual_sequence_exports USING btree (coverage_gap_id, created_at DESC);


--
-- Name: idx_manual_sequence_exports_route; Type: INDEX; Schema: route_work; Owner: -
--

CREATE INDEX idx_manual_sequence_exports_route ON route_work.manual_sequence_exports USING btree (route_id, created_at DESC);


--
-- Name: idx_manual_sequence_exports_source; Type: INDEX; Schema: route_work; Owner: -
--

CREATE INDEX idx_manual_sequence_exports_source ON route_work.manual_sequence_exports USING btree (source, created_at DESC);


--
-- Name: idx_relation_stop_prior_osm_ref; Type: INDEX; Schema: route_work; Owner: -
--

CREATE INDEX idx_relation_stop_prior_osm_ref ON route_work.relation_stop_prior USING btree (osm_ref);


--
-- Name: idx_relation_stop_prior_route; Type: INDEX; Schema: route_work; Owner: -
--

CREATE INDEX idx_relation_stop_prior_route ON route_work.relation_stop_prior USING btree (route_id);


--
-- Name: idx_route_approvals_geom; Type: INDEX; Schema: route_work; Owner: -
--

CREATE INDEX idx_route_approvals_geom ON route_work.route_approvals USING btree (chosen_geometry_candidate_id);


--
-- Name: idx_sequence_approvals_candidate; Type: INDEX; Schema: route_work; Owner: -
--

CREATE INDEX idx_sequence_approvals_candidate ON route_work.sequence_approvals USING btree (chosen_stop_sequence_candidate_id);


--
-- Name: idx_sequence_approvals_set; Type: INDEX; Schema: route_work; Owner: -
--

CREATE INDEX idx_sequence_approvals_set ON route_work.sequence_approvals USING btree (stop_sequence_set_id);


--
-- Name: idx_stop_sequence_candidates_set_rank; Type: INDEX; Schema: route_work; Owner: -
--

CREATE INDEX idx_stop_sequence_candidates_set_rank ON route_work.stop_sequence_candidates USING btree (set_id, rank);


--
-- Name: uq_route_approvals_route; Type: INDEX; Schema: route_work; Owner: -
--

CREATE UNIQUE INDEX uq_route_approvals_route ON route_work.route_approvals USING btree (route_id);


--
-- Name: uq_sequence_approvals_route; Type: INDEX; Schema: route_work; Owner: -
--

CREATE UNIQUE INDEX uq_sequence_approvals_route ON route_work.sequence_approvals USING btree (route_id);


--
-- Name: idx_rer_bbox_gist; Type: INDEX; Schema: semantics; Owner: -
--

CREATE INDEX idx_rer_bbox_gist ON semantics.route_evidence_records USING gist (bbox);


--
-- Name: idx_rer_geom_gist; Type: INDEX; Schema: semantics; Owner: -
--

CREATE INDEX idx_rer_geom_gist ON semantics.route_evidence_records USING gist (geom);


--
-- Name: idx_rer_route_hint; Type: INDEX; Schema: semantics; Owner: -
--

CREATE INDEX idx_rer_route_hint ON semantics.route_evidence_records USING btree (route_id_hint);


--
-- Name: idx_rnc_route_generated; Type: INDEX; Schema: semantics; Owner: -
--

CREATE INDEX idx_rnc_route_generated ON semantics.route_name_candidates USING btree (route_id, generated_at DESC, rank_pos);


--
-- Name: idx_rnc_route_run; Type: INDEX; Schema: semantics; Owner: -
--

CREATE INDEX idx_rnc_route_run ON semantics.route_name_candidates USING btree (route_id, run_id, rank_pos);


--
-- Name: idx_rnf_route_created; Type: INDEX; Schema: semantics; Owner: -
--

CREATE INDEX idx_rnf_route_created ON semantics.route_name_feedback USING btree (route_id, created_at DESC);


--
-- Name: idx_rnsr_route_created; Type: INDEX; Schema: semantics; Owner: -
--

CREATE INDEX idx_rnsr_route_created ON semantics.route_name_seed_runs USING btree (route_id, created_at DESC);


--
-- Name: uq_rnf_route_winner; Type: INDEX; Schema: semantics; Owner: -
--

CREATE UNIQUE INDEX uq_rnf_route_winner ON semantics.route_name_feedback USING btree (route_id, reviewer) WHERE (is_winner = true);


--
-- Name: route_semantics trg_notify_route_approved; Type: TRIGGER; Schema: catalog; Owner: -
--

CREATE TRIGGER trg_notify_route_approved AFTER UPDATE OF approved ON catalog.route_semantics FOR EACH ROW EXECUTE FUNCTION catalog.fn_notify_route_approved();


--
-- Name: route_semantics trg_notify_route_approved_insert; Type: TRIGGER; Schema: catalog; Owner: -
--

CREATE TRIGGER trg_notify_route_approved_insert AFTER INSERT ON catalog.route_semantics FOR EACH ROW WHEN ((new.approved = true)) EXECUTE FUNCTION catalog.fn_notify_route_approved();


--
-- Name: route_semantics trg_route_semantics_updated_at; Type: TRIGGER; Schema: catalog; Owner: -
--

CREATE TRIGGER trg_route_semantics_updated_at BEFORE UPDATE ON catalog.route_semantics FOR EACH ROW EXECUTE FUNCTION catalog.fn_update_timestamp();


--
-- Name: users trg_console_users_updated_at; Type: TRIGGER; Schema: console; Owner: -
--

CREATE TRIGGER trg_console_users_updated_at BEFORE UPDATE ON console.users FOR EACH ROW EXECUTE FUNCTION console.set_updated_at();


--
-- Name: orchestrator_sessions trg_orch_sessions_updated_at; Type: TRIGGER; Schema: console; Owner: -
--

CREATE TRIGGER trg_orch_sessions_updated_at BEFORE UPDATE ON console.orchestrator_sessions FOR EACH ROW EXECUTE FUNCTION console.set_updated_at();


--
-- Name: places trg_place_history; Type: TRIGGER; Schema: geo_prod; Owner: -
--

CREATE TRIGGER trg_place_history BEFORE UPDATE ON geo_prod.places FOR EACH ROW EXECUTE FUNCTION geo_prod.capture_place_history();


--
-- Name: gtfs_agency trg_sync_gtfs_id_agency; Type: TRIGGER; Schema: gtfs_work; Owner: -
--

CREATE TRIGGER trg_sync_gtfs_id_agency BEFORE INSERT OR UPDATE ON gtfs_work.gtfs_agency FOR EACH ROW EXECUTE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run();


--
-- Name: gtfs_calendar trg_sync_gtfs_id_calendar; Type: TRIGGER; Schema: gtfs_work; Owner: -
--

CREATE TRIGGER trg_sync_gtfs_id_calendar BEFORE INSERT OR UPDATE ON gtfs_work.gtfs_calendar FOR EACH ROW EXECUTE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run();


--
-- Name: gtfs_calendar_dates trg_sync_gtfs_id_calendar_dates; Type: TRIGGER; Schema: gtfs_work; Owner: -
--

CREATE TRIGGER trg_sync_gtfs_id_calendar_dates BEFORE INSERT OR UPDATE ON gtfs_work.gtfs_calendar_dates FOR EACH ROW EXECUTE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run();


--
-- Name: gtfs_frequencies trg_sync_gtfs_id_frequencies; Type: TRIGGER; Schema: gtfs_work; Owner: -
--

CREATE TRIGGER trg_sync_gtfs_id_frequencies BEFORE INSERT OR UPDATE ON gtfs_work.gtfs_frequencies FOR EACH ROW EXECUTE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run();


--
-- Name: gtfs_overrides trg_sync_gtfs_id_overrides; Type: TRIGGER; Schema: gtfs_work; Owner: -
--

CREATE TRIGGER trg_sync_gtfs_id_overrides BEFORE INSERT OR UPDATE ON gtfs_work.gtfs_overrides FOR EACH ROW EXECUTE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run();


--
-- Name: revision_events trg_sync_gtfs_id_revision_events; Type: TRIGGER; Schema: gtfs_work; Owner: -
--

CREATE TRIGGER trg_sync_gtfs_id_revision_events BEFORE INSERT OR UPDATE ON gtfs_work.revision_events FOR EACH ROW EXECUTE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run();


--
-- Name: gtfs_routes trg_sync_gtfs_id_routes; Type: TRIGGER; Schema: gtfs_work; Owner: -
--

CREATE TRIGGER trg_sync_gtfs_id_routes BEFORE INSERT OR UPDATE ON gtfs_work.gtfs_routes FOR EACH ROW EXECUTE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run();


--
-- Name: gtfs_shapes trg_sync_gtfs_id_shapes; Type: TRIGGER; Schema: gtfs_work; Owner: -
--

CREATE TRIGGER trg_sync_gtfs_id_shapes BEFORE INSERT OR UPDATE ON gtfs_work.gtfs_shapes FOR EACH ROW EXECUTE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run();


--
-- Name: gtfs_stop_times trg_sync_gtfs_id_stop_times; Type: TRIGGER; Schema: gtfs_work; Owner: -
--

CREATE TRIGGER trg_sync_gtfs_id_stop_times BEFORE INSERT OR UPDATE ON gtfs_work.gtfs_stop_times FOR EACH ROW EXECUTE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run();


--
-- Name: gtfs_stops trg_sync_gtfs_id_stops; Type: TRIGGER; Schema: gtfs_work; Owner: -
--

CREATE TRIGGER trg_sync_gtfs_id_stops BEFORE INSERT OR UPDATE ON gtfs_work.gtfs_stops FOR EACH ROW EXECUTE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run();


--
-- Name: gtfs_trips trg_sync_gtfs_id_trips; Type: TRIGGER; Schema: gtfs_work; Owner: -
--

CREATE TRIGGER trg_sync_gtfs_id_trips BEFORE INSERT OR UPDATE ON gtfs_work.gtfs_trips FOR EACH ROW EXECUTE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run();


--
-- Name: upload_runs trg_sync_gtfs_id_upload_runs; Type: TRIGGER; Schema: gtfs_work; Owner: -
--

CREATE TRIGGER trg_sync_gtfs_id_upload_runs BEFORE INSERT OR UPDATE ON gtfs_work.upload_runs FOR EACH ROW EXECUTE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run();


--
-- Name: routes trg_route_prod_touch; Type: TRIGGER; Schema: route_prod; Owner: -
--

CREATE TRIGGER trg_route_prod_touch BEFORE UPDATE ON route_prod.routes FOR EACH ROW EXECUTE FUNCTION route_prod.touch_updated_at();


--
-- Name: routes trg_route_prod_touch_cleanliness; Type: TRIGGER; Schema: route_prod; Owner: -
--

CREATE TRIGGER trg_route_prod_touch_cleanliness BEFORE UPDATE ON route_prod.routes FOR EACH ROW EXECUTE FUNCTION route_prod.touch_cleanliness_classified_at();


--
-- Name: route_semantics trg_route_semantics_search_tsv_sync; Type: TRIGGER; Schema: route_prod; Owner: -
--

CREATE TRIGGER trg_route_semantics_search_tsv_sync BEFORE INSERT OR UPDATE OF route_name, route_aliases, landmark_tags ON route_prod.route_semantics FOR EACH ROW EXECUTE FUNCTION semantics.route_semantics_search_tsv_sync();


--
-- Name: route_semantics trg_route_semantics_sync_direction_context; Type: TRIGGER; Schema: route_prod; Owner: -
--

CREATE TRIGGER trg_route_semantics_sync_direction_context BEFORE INSERT OR UPDATE OF route_id ON route_prod.route_semantics FOR EACH ROW EXECUTE FUNCTION semantics.sync_route_semantics_direction_context();


--
-- Name: routes trg_routes_enforce_audit_trail; Type: TRIGGER; Schema: route_prod; Owner: -
--

CREATE TRIGGER trg_routes_enforce_audit_trail BEFORE INSERT OR UPDATE ON route_prod.routes FOR EACH ROW EXECUTE FUNCTION route_prod.trg_routes_enforce_audit_trail();


--
-- Name: coverage_gaps trg_route_review_touch_coverage_gaps; Type: TRIGGER; Schema: route_review; Owner: -
--

CREATE TRIGGER trg_route_review_touch_coverage_gaps BEFORE UPDATE ON route_review.coverage_gaps FOR EACH ROW EXECUTE FUNCTION route_review.touch_updated_at();


--
-- Name: route_job_dedupe_groups trg_route_review_touch_dedupe_groups; Type: TRIGGER; Schema: route_review; Owner: -
--

CREATE TRIGGER trg_route_review_touch_dedupe_groups BEFORE UPDATE ON route_review.route_job_dedupe_groups FOR EACH ROW EXECUTE FUNCTION route_review.touch_updated_at();


--
-- Name: route_job_dedupe_memberships trg_route_review_touch_dedupe_memberships; Type: TRIGGER; Schema: route_review; Owner: -
--

CREATE TRIGGER trg_route_review_touch_dedupe_memberships BEFORE UPDATE ON route_review.route_job_dedupe_memberships FOR EACH ROW EXECUTE FUNCTION route_review.touch_updated_at();


--
-- Name: inverse_direction_status trg_inverse_direction_status_touch; Type: TRIGGER; Schema: route_work; Owner: -
--

CREATE TRIGGER trg_inverse_direction_status_touch BEFORE UPDATE ON route_work.inverse_direction_status FOR EACH ROW EXECUTE FUNCTION route_work.touch_inverse_direction_status_updated_at();


--
-- Name: sequence_approvals trg_sequence_approvals_touch; Type: TRIGGER; Schema: route_work; Owner: -
--

CREATE TRIGGER trg_sequence_approvals_touch BEFORE UPDATE ON route_work.sequence_approvals FOR EACH ROW EXECUTE FUNCTION route_work.touch_sequence_approval_updated_at();


--
-- Name: ai_label_events ai_label_events_suggestion_id_fkey; Type: FK CONSTRAINT; Schema: ai; Owner: -
--

ALTER TABLE ONLY ai.ai_label_events
    ADD CONSTRAINT ai_label_events_suggestion_id_fkey FOREIGN KEY (suggestion_id) REFERENCES ai.ai_suggestions(suggestion_id) ON DELETE CASCADE;


--
-- Name: ai_training_dataset ai_training_dataset_source_suggestion_id_fkey; Type: FK CONSTRAINT; Schema: ai; Owner: -
--

ALTER TABLE ONLY ai.ai_training_dataset
    ADD CONSTRAINT ai_training_dataset_source_suggestion_id_fkey FOREIGN KEY (source_suggestion_id) REFERENCES ai.ai_suggestions(suggestion_id) ON DELETE SET NULL;


--
-- Name: route_layover_policy route_layover_policy_route_id_fkey; Type: FK CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.route_layover_policy
    ADD CONSTRAINT route_layover_policy_route_id_fkey FOREIGN KEY (route_id) REFERENCES catalog.route_semantics(route_id) ON DELETE CASCADE;


--
-- Name: route_schedule_profile route_schedule_profile_route_id_fkey; Type: FK CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.route_schedule_profile
    ADD CONSTRAINT route_schedule_profile_route_id_fkey FOREIGN KEY (route_id) REFERENCES catalog.route_semantics(route_id) ON DELETE CASCADE;


--
-- Name: route_semantics route_semantics_route_id_fkey; Type: FK CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.route_semantics
    ADD CONSTRAINT route_semantics_route_id_fkey FOREIGN KEY (route_id) REFERENCES route_prod.routes(route_id) ON DELETE RESTRICT;


--
-- Name: route_service_days route_service_days_route_id_fkey; Type: FK CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.route_service_days
    ADD CONSTRAINT route_service_days_route_id_fkey FOREIGN KEY (route_id) REFERENCES catalog.route_semantics(route_id) ON DELETE CASCADE;


--
-- Name: route_service_exceptions route_service_exceptions_route_id_fkey; Type: FK CONSTRAINT; Schema: catalog; Owner: -
--

ALTER TABLE ONLY catalog.route_service_exceptions
    ADD CONSTRAINT route_service_exceptions_route_id_fkey FOREIGN KEY (route_id) REFERENCES catalog.route_semantics(route_id) ON DELETE CASCADE;


--
-- Name: api_keys api_keys_user_id_fkey; Type: FK CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.api_keys
    ADD CONSTRAINT api_keys_user_id_fkey FOREIGN KEY (user_id) REFERENCES console.users(user_id) ON DELETE CASCADE;


--
-- Name: approval_queue approval_queue_decided_by_fkey; Type: FK CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.approval_queue
    ADD CONSTRAINT approval_queue_decided_by_fkey FOREIGN KEY (decided_by) REFERENCES console.users(user_id) ON DELETE SET NULL;


--
-- Name: approval_queue approval_queue_session_id_fkey; Type: FK CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.approval_queue
    ADD CONSTRAINT approval_queue_session_id_fkey FOREIGN KEY (session_id) REFERENCES console.orchestrator_sessions(session_id) ON DELETE CASCADE;


--
-- Name: approval_queue approval_queue_step_id_fkey; Type: FK CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.approval_queue
    ADD CONSTRAINT approval_queue_step_id_fkey FOREIGN KEY (step_id) REFERENCES console.orchestrator_steps(step_id) ON DELETE CASCADE;


--
-- Name: audit_events audit_events_user_id_fkey; Type: FK CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.audit_events
    ADD CONSTRAINT audit_events_user_id_fkey FOREIGN KEY (user_id) REFERENCES console.users(user_id) ON DELETE SET NULL;


--
-- Name: email_verification_tokens email_verification_tokens_requested_by_fkey; Type: FK CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.email_verification_tokens
    ADD CONSTRAINT email_verification_tokens_requested_by_fkey FOREIGN KEY (requested_by) REFERENCES console.users(user_id) ON DELETE SET NULL;


--
-- Name: email_verification_tokens email_verification_tokens_user_id_fkey; Type: FK CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.email_verification_tokens
    ADD CONSTRAINT email_verification_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES console.users(user_id) ON DELETE CASCADE;


--
-- Name: export_jobs export_jobs_user_id_fkey; Type: FK CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.export_jobs
    ADD CONSTRAINT export_jobs_user_id_fkey FOREIGN KEY (user_id) REFERENCES console.users(user_id) ON DELETE SET NULL;


--
-- Name: orchestrator_sessions orchestrator_sessions_user_id_fkey; Type: FK CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.orchestrator_sessions
    ADD CONSTRAINT orchestrator_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES console.users(user_id) ON DELETE SET NULL;


--
-- Name: orchestrator_steps orchestrator_steps_session_id_fkey; Type: FK CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.orchestrator_steps
    ADD CONSTRAINT orchestrator_steps_session_id_fkey FOREIGN KEY (session_id) REFERENCES console.orchestrator_sessions(session_id) ON DELETE CASCADE;


--
-- Name: phase_decisions phase_decisions_user_id_fkey; Type: FK CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.phase_decisions
    ADD CONSTRAINT phase_decisions_user_id_fkey FOREIGN KEY (user_id) REFERENCES console.users(user_id) ON DELETE SET NULL;


--
-- Name: sessions sessions_user_id_fkey; Type: FK CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.sessions
    ADD CONSTRAINT sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES console.users(user_id) ON DELETE CASCADE;


--
-- Name: workspace_state workspace_state_user_id_fkey; Type: FK CONSTRAINT; Schema: console; Owner: -
--

ALTER TABLE ONLY console.workspace_state
    ADD CONSTRAINT workspace_state_user_id_fkey FOREIGN KEY (user_id) REFERENCES console.users(user_id) ON DELETE CASCADE;


--
-- Name: node_place_map node_place_map_place_id_fkey; Type: FK CONSTRAINT; Schema: geo_prod; Owner: -
--

ALTER TABLE ONLY geo_prod.node_place_map
    ADD CONSTRAINT node_place_map_place_id_fkey FOREIGN KEY (place_id) REFERENCES geo_prod.places(place_id) ON DELETE RESTRICT;


--
-- Name: place_alias_embeddings place_alias_embeddings_alias_id_fkey; Type: FK CONSTRAINT; Schema: geo_prod; Owner: -
--

ALTER TABLE ONLY geo_prod.place_alias_embeddings
    ADD CONSTRAINT place_alias_embeddings_alias_id_fkey FOREIGN KEY (alias_id) REFERENCES geo_prod.place_aliases(alias_id) ON DELETE CASCADE;


--
-- Name: place_alias_embeddings place_alias_embeddings_place_id_fkey; Type: FK CONSTRAINT; Schema: geo_prod; Owner: -
--

ALTER TABLE ONLY geo_prod.place_alias_embeddings
    ADD CONSTRAINT place_alias_embeddings_place_id_fkey FOREIGN KEY (place_id) REFERENCES geo_prod.places(place_id) ON DELETE CASCADE;


--
-- Name: place_aliases place_aliases_place_id_fkey; Type: FK CONSTRAINT; Schema: geo_prod; Owner: -
--

ALTER TABLE ONLY geo_prod.place_aliases
    ADD CONSTRAINT place_aliases_place_id_fkey FOREIGN KEY (place_id) REFERENCES geo_prod.places(place_id) ON DELETE CASCADE;


--
-- Name: place_embeddings place_embeddings_place_id_fkey; Type: FK CONSTRAINT; Schema: geo_prod; Owner: -
--

ALTER TABLE ONLY geo_prod.place_embeddings
    ADD CONSTRAINT place_embeddings_place_id_fkey FOREIGN KEY (place_id) REFERENCES geo_prod.places(place_id) ON DELETE CASCADE;


--
-- Name: name_evidence name_evidence_extract_run_id_fkey; Type: FK CONSTRAINT; Schema: geo_raw; Owner: -
--

ALTER TABLE ONLY geo_raw.name_evidence
    ADD CONSTRAINT name_evidence_extract_run_id_fkey FOREIGN KEY (extract_run_id) REFERENCES geo_raw.extract_runs(extract_run_id) ON DELETE CASCADE;


--
-- Name: alias_candidates alias_candidates_place_candidate_id_fkey; Type: FK CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.alias_candidates
    ADD CONSTRAINT alias_candidates_place_candidate_id_fkey FOREIGN KEY (place_candidate_id) REFERENCES geo_work.place_candidates(place_candidate_id) ON DELETE CASCADE;


--
-- Name: node_geo_context node_geo_context_extract_run_id_fkey; Type: FK CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.node_geo_context
    ADD CONSTRAINT node_geo_context_extract_run_id_fkey FOREIGN KEY (extract_run_id) REFERENCES geo_raw.extract_runs(extract_run_id) ON DELETE CASCADE;


--
-- Name: node_place_map_work node_place_map_work_place_candidate_id_fkey; Type: FK CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.node_place_map_work
    ADD CONSTRAINT node_place_map_work_place_candidate_id_fkey FOREIGN KEY (place_candidate_id) REFERENCES geo_work.place_candidates(place_candidate_id) ON DELETE CASCADE;


--
-- Name: node_place_map_work node_place_map_work_place_set_id_fkey; Type: FK CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.node_place_map_work
    ADD CONSTRAINT node_place_map_work_place_set_id_fkey FOREIGN KEY (place_set_id) REFERENCES geo_work.place_candidate_sets(place_set_id) ON DELETE CASCADE;


--
-- Name: place_candidate_sets place_candidate_sets_source_extract_run_id_fkey; Type: FK CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.place_candidate_sets
    ADD CONSTRAINT place_candidate_sets_source_extract_run_id_fkey FOREIGN KEY (source_extract_run_id) REFERENCES geo_raw.extract_runs(extract_run_id) ON DELETE CASCADE;


--
-- Name: place_candidates place_candidates_place_set_id_fkey; Type: FK CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.place_candidates
    ADD CONSTRAINT place_candidates_place_set_id_fkey FOREIGN KEY (place_set_id) REFERENCES geo_work.place_candidate_sets(place_set_id) ON DELETE CASCADE;


--
-- Name: place_name_candidates place_name_candidates_place_candidate_id_fkey; Type: FK CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.place_name_candidates
    ADD CONSTRAINT place_name_candidates_place_candidate_id_fkey FOREIGN KEY (place_candidate_id) REFERENCES geo_work.place_candidates(place_candidate_id) ON DELETE CASCADE;


--
-- Name: place_name_candidates place_name_candidates_place_set_id_fkey; Type: FK CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.place_name_candidates
    ADD CONSTRAINT place_name_candidates_place_set_id_fkey FOREIGN KEY (place_set_id) REFERENCES geo_work.place_candidate_sets(place_set_id) ON DELETE CASCADE;


--
-- Name: place_name_feedback place_name_feedback_chosen_name_candidate_id_fkey; Type: FK CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.place_name_feedback
    ADD CONSTRAINT place_name_feedback_chosen_name_candidate_id_fkey FOREIGN KEY (chosen_name_candidate_id) REFERENCES geo_work.place_name_candidates(name_candidate_id) ON DELETE SET NULL;


--
-- Name: place_name_feedback place_name_feedback_place_candidate_id_fkey; Type: FK CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.place_name_feedback
    ADD CONSTRAINT place_name_feedback_place_candidate_id_fkey FOREIGN KEY (place_candidate_id) REFERENCES geo_work.place_candidates(place_candidate_id) ON DELETE CASCADE;


--
-- Name: place_name_feedback place_name_feedback_place_set_id_fkey; Type: FK CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.place_name_feedback
    ADD CONSTRAINT place_name_feedback_place_set_id_fkey FOREIGN KEY (place_set_id) REFERENCES geo_work.place_candidate_sets(place_set_id) ON DELETE CASCADE;


--
-- Name: place_set_metrics place_set_metrics_place_set_id_fkey; Type: FK CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.place_set_metrics
    ADD CONSTRAINT place_set_metrics_place_set_id_fkey FOREIGN KEY (place_set_id) REFERENCES geo_work.place_candidate_sets(place_set_id) ON DELETE CASCADE;


--
-- Name: poi_stop_feedback poi_stop_feedback_place_candidate_id_fkey; Type: FK CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.poi_stop_feedback
    ADD CONSTRAINT poi_stop_feedback_place_candidate_id_fkey FOREIGN KEY (place_candidate_id) REFERENCES geo_work.place_candidates(place_candidate_id) ON DELETE CASCADE;


--
-- Name: poi_stop_feedback poi_stop_feedback_place_set_id_fkey; Type: FK CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.poi_stop_feedback
    ADD CONSTRAINT poi_stop_feedback_place_set_id_fkey FOREIGN KEY (place_set_id) REFERENCES geo_work.place_candidate_sets(place_set_id) ON DELETE CASCADE;


--
-- Name: selection_log selection_log_chosen_set_id_fkey; Type: FK CONSTRAINT; Schema: geo_work; Owner: -
--

ALTER TABLE ONLY geo_work.selection_log
    ADD CONSTRAINT selection_log_chosen_set_id_fkey FOREIGN KEY (chosen_set_id) REFERENCES geo_work.place_candidate_sets(place_set_id) ON DELETE RESTRICT;


--
-- Name: gtfs_audit_log gtfs_audit_log_artifact_id_fkey; Type: FK CONSTRAINT; Schema: gtfs; Owner: -
--

ALTER TABLE ONLY gtfs.gtfs_audit_log
    ADD CONSTRAINT gtfs_audit_log_artifact_id_fkey FOREIGN KEY (artifact_id) REFERENCES gtfs.gtfs_artifacts(artifact_id) ON DELETE SET NULL;


--
-- Name: gtfs_notifications gtfs_notifications_artifact_id_fkey; Type: FK CONSTRAINT; Schema: gtfs; Owner: -
--

ALTER TABLE ONLY gtfs.gtfs_notifications
    ADD CONSTRAINT gtfs_notifications_artifact_id_fkey FOREIGN KEY (artifact_id) REFERENCES gtfs.gtfs_artifacts(artifact_id) ON DELETE CASCADE;


--
-- Name: feed_versions feed_versions_export_run_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_prod; Owner: -
--

ALTER TABLE ONLY gtfs_prod.feed_versions
    ADD CONSTRAINT feed_versions_export_run_id_fkey FOREIGN KEY (export_run_id) REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE RESTRICT;


--
-- Name: agency_match_requests agency_match_requests_resolved_agency_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.agency_match_requests
    ADD CONSTRAINT agency_match_requests_resolved_agency_id_fkey FOREIGN KEY (resolved_agency_id) REFERENCES gtfs_work.agency_catalog(agency_id) ON DELETE SET NULL;


--
-- Name: agency_match_requests agency_match_requests_route_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.agency_match_requests
    ADD CONSTRAINT agency_match_requests_route_id_fkey FOREIGN KEY (route_id) REFERENCES route_prod.routes(route_id) ON DELETE CASCADE;


--
-- Name: agency_match_requests agency_match_requests_suggested_agency_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.agency_match_requests
    ADD CONSTRAINT agency_match_requests_suggested_agency_id_fkey FOREIGN KEY (suggested_agency_id) REFERENCES gtfs_work.agency_catalog(agency_id) ON DELETE SET NULL;


--
-- Name: calendar_exceptions calendar_exceptions_profile_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.calendar_exceptions
    ADD CONSTRAINT calendar_exceptions_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES gtfs_work.route_schedule_profiles(profile_id) ON DELETE CASCADE;


--
-- Name: gtfs_agency gtfs_agency_export_run_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.gtfs_agency
    ADD CONSTRAINT gtfs_agency_export_run_id_fkey FOREIGN KEY (export_run_id) REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE;


--
-- Name: gtfs_builds gtfs_builds_export_run_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.gtfs_builds
    ADD CONSTRAINT gtfs_builds_export_run_id_fkey FOREIGN KEY (export_run_id) REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE SET NULL;


--
-- Name: gtfs_calendar_dates gtfs_calendar_dates_export_run_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.gtfs_calendar_dates
    ADD CONSTRAINT gtfs_calendar_dates_export_run_id_fkey FOREIGN KEY (export_run_id) REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE;


--
-- Name: gtfs_calendar gtfs_calendar_export_run_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.gtfs_calendar
    ADD CONSTRAINT gtfs_calendar_export_run_id_fkey FOREIGN KEY (export_run_id) REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE;


--
-- Name: gtfs_frequencies gtfs_frequencies_export_run_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.gtfs_frequencies
    ADD CONSTRAINT gtfs_frequencies_export_run_id_fkey FOREIGN KEY (export_run_id) REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE;


--
-- Name: gtfs_loadings gtfs_loadings_export_run_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.gtfs_loadings
    ADD CONSTRAINT gtfs_loadings_export_run_id_fkey FOREIGN KEY (export_run_id) REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE;


--
-- Name: gtfs_overrides gtfs_overrides_export_run_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.gtfs_overrides
    ADD CONSTRAINT gtfs_overrides_export_run_id_fkey FOREIGN KEY (export_run_id) REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE;


--
-- Name: gtfs_routes gtfs_routes_export_run_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.gtfs_routes
    ADD CONSTRAINT gtfs_routes_export_run_id_fkey FOREIGN KEY (export_run_id) REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE;


--
-- Name: gtfs_shapes gtfs_shapes_export_run_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.gtfs_shapes
    ADD CONSTRAINT gtfs_shapes_export_run_id_fkey FOREIGN KEY (export_run_id) REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE;


--
-- Name: gtfs_stop_times gtfs_stop_times_export_run_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.gtfs_stop_times
    ADD CONSTRAINT gtfs_stop_times_export_run_id_fkey FOREIGN KEY (export_run_id) REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE;


--
-- Name: gtfs_stops gtfs_stops_export_run_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.gtfs_stops
    ADD CONSTRAINT gtfs_stops_export_run_id_fkey FOREIGN KEY (export_run_id) REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE;


--
-- Name: gtfs_trips gtfs_trips_export_run_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.gtfs_trips
    ADD CONSTRAINT gtfs_trips_export_run_id_fkey FOREIGN KEY (export_run_id) REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE;


--
-- Name: revision_events revision_events_export_run_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.revision_events
    ADD CONSTRAINT revision_events_export_run_id_fkey FOREIGN KEY (export_run_id) REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE;


--
-- Name: route_agency_links route_agency_links_agency_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.route_agency_links
    ADD CONSTRAINT route_agency_links_agency_id_fkey FOREIGN KEY (agency_id) REFERENCES gtfs_work.agency_catalog(agency_id) ON DELETE RESTRICT;


--
-- Name: route_agency_links route_agency_links_route_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.route_agency_links
    ADD CONSTRAINT route_agency_links_route_id_fkey FOREIGN KEY (route_id) REFERENCES route_prod.routes(route_id) ON DELETE CASCADE;


--
-- Name: route_runtime_estimate_bindings route_runtime_estimate_bindings_estimate_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.route_runtime_estimate_bindings
    ADD CONSTRAINT route_runtime_estimate_bindings_estimate_id_fkey FOREIGN KEY (estimate_id) REFERENCES gtfs_work.runtime_route_estimates(estimate_id) ON DELETE CASCADE;


--
-- Name: route_runtime_estimate_bindings route_runtime_estimate_bindings_route_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.route_runtime_estimate_bindings
    ADD CONSTRAINT route_runtime_estimate_bindings_route_id_fkey FOREIGN KEY (route_id) REFERENCES route_prod.routes(route_id) ON DELETE CASCADE;


--
-- Name: route_schedule_profiles route_schedule_profiles_route_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.route_schedule_profiles
    ADD CONSTRAINT route_schedule_profiles_route_id_fkey FOREIGN KEY (route_id) REFERENCES route_prod.routes(route_id) ON DELETE CASCADE;


--
-- Name: runtime_route_estimates runtime_route_estimates_route_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.runtime_route_estimates
    ADD CONSTRAINT runtime_route_estimates_route_id_fkey FOREIGN KEY (route_id) REFERENCES route_prod.routes(route_id) ON DELETE CASCADE;


--
-- Name: runtime_route_leg_features runtime_route_leg_features_estimate_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.runtime_route_leg_features
    ADD CONSTRAINT runtime_route_leg_features_estimate_id_fkey FOREIGN KEY (estimate_id) REFERENCES gtfs_work.runtime_route_estimates(estimate_id) ON DELETE CASCADE;


--
-- Name: service_windows service_windows_profile_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.service_windows
    ADD CONSTRAINT service_windows_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES gtfs_work.route_schedule_profiles(profile_id) ON DELETE CASCADE;


--
-- Name: trip_departures trip_departures_export_run_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.trip_departures
    ADD CONSTRAINT trip_departures_export_run_id_fkey FOREIGN KEY (export_run_id) REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE;


--
-- Name: upload_runs upload_runs_export_run_id_fkey; Type: FK CONSTRAINT; Schema: gtfs_work; Owner: -
--

ALTER TABLE ONLY gtfs_work.upload_runs
    ADD CONSTRAINT upload_runs_export_run_id_fkey FOREIGN KEY (export_run_id) REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE;


--
-- Name: nodes nodes_superseded_by_fkey; Type: FK CONSTRAINT; Schema: node_prod; Owner: -
--

ALTER TABLE ONLY node_prod.nodes
    ADD CONSTRAINT nodes_superseded_by_fkey FOREIGN KEY (superseded_by) REFERENCES node_prod.nodes(node_id) ON DELETE SET NULL;


--
-- Name: synthesis_events synthesis_events_node_id_fkey; Type: FK CONSTRAINT; Schema: node_prod; Owner: -
--

ALTER TABLE ONLY node_prod.synthesis_events
    ADD CONSTRAINT synthesis_events_node_id_fkey FOREIGN KEY (node_id) REFERENCES node_prod.nodes(node_id) ON DELETE SET NULL;


--
-- Name: synthesis_events synthesis_events_route_id_fkey; Type: FK CONSTRAINT; Schema: node_prod; Owner: -
--

ALTER TABLE ONLY node_prod.synthesis_events
    ADD CONSTRAINT synthesis_events_route_id_fkey FOREIGN KEY (route_id) REFERENCES route_prod.routes(route_id) ON DELETE SET NULL;


--
-- Name: overpass_elements overpass_elements_run_id_fkey; Type: FK CONSTRAINT; Schema: node_raw; Owner: -
--

ALTER TABLE ONLY node_raw.overpass_elements
    ADD CONSTRAINT overpass_elements_run_id_fkey FOREIGN KEY (run_id) REFERENCES node_raw.overpass_runs(run_id) ON DELETE CASCADE;


--
-- Name: overpass_queries overpass_queries_action_id_fkey; Type: FK CONSTRAINT; Schema: node_raw; Owner: -
--

ALTER TABLE ONLY node_raw.overpass_queries
    ADD CONSTRAINT overpass_queries_action_id_fkey FOREIGN KEY (action_id) REFERENCES node_raw.overpass_actions(action_id) ON DELETE RESTRICT;


--
-- Name: overpass_runs overpass_runs_query_id_fkey; Type: FK CONSTRAINT; Schema: node_raw; Owner: -
--

ALTER TABLE ONLY node_raw.overpass_runs
    ADD CONSTRAINT overpass_runs_query_id_fkey FOREIGN KEY (query_id) REFERENCES node_raw.overpass_queries(query_id) ON DELETE CASCADE;


--
-- Name: node_candidates node_candidates_node_set_id_fkey; Type: FK CONSTRAINT; Schema: node_work; Owner: -
--

ALTER TABLE ONLY node_work.node_candidates
    ADD CONSTRAINT node_candidates_node_set_id_fkey FOREIGN KEY (node_set_id) REFERENCES node_work.node_candidate_sets(node_set_id) ON DELETE CASCADE;


--
-- Name: node_candidates node_candidates_source_run_id_fkey; Type: FK CONSTRAINT; Schema: node_work; Owner: -
--

ALTER TABLE ONLY node_work.node_candidates
    ADD CONSTRAINT node_candidates_source_run_id_fkey FOREIGN KEY (source_run_id) REFERENCES node_raw.overpass_runs(run_id) ON DELETE CASCADE;


--
-- Name: node_clusters node_clusters_node_candidate_id_fkey; Type: FK CONSTRAINT; Schema: node_work; Owner: -
--

ALTER TABLE ONLY node_work.node_clusters
    ADD CONSTRAINT node_clusters_node_candidate_id_fkey FOREIGN KEY (node_candidate_id) REFERENCES node_work.node_candidates(node_candidate_id) ON DELETE CASCADE;


--
-- Name: node_clusters node_clusters_node_set_id_fkey; Type: FK CONSTRAINT; Schema: node_work; Owner: -
--

ALTER TABLE ONLY node_work.node_clusters
    ADD CONSTRAINT node_clusters_node_set_id_fkey FOREIGN KEY (node_set_id) REFERENCES node_work.node_candidate_sets(node_set_id) ON DELETE CASCADE;


--
-- Name: node_features node_features_node_candidate_id_fkey; Type: FK CONSTRAINT; Schema: node_work; Owner: -
--

ALTER TABLE ONLY node_work.node_features
    ADD CONSTRAINT node_features_node_candidate_id_fkey FOREIGN KEY (node_candidate_id) REFERENCES node_work.node_candidates(node_candidate_id) ON DELETE CASCADE;


--
-- Name: nodes_resolved nodes_resolved_chosen_candidate_id_fkey; Type: FK CONSTRAINT; Schema: node_work; Owner: -
--

ALTER TABLE ONLY node_work.nodes_resolved
    ADD CONSTRAINT nodes_resolved_chosen_candidate_id_fkey FOREIGN KEY (chosen_candidate_id) REFERENCES node_work.node_candidates(node_candidate_id) ON DELETE RESTRICT;


--
-- Name: nodes_resolved nodes_resolved_node_set_id_fkey; Type: FK CONSTRAINT; Schema: node_work; Owner: -
--

ALTER TABLE ONLY node_work.nodes_resolved
    ADD CONSTRAINT nodes_resolved_node_set_id_fkey FOREIGN KEY (node_set_id) REFERENCES node_work.node_candidate_sets(node_set_id) ON DELETE CASCADE;


--
-- Name: re_entry_queue fk_re_entry_queue_route; Type: FK CONSTRAINT; Schema: route_prod; Owner: -
--

ALTER TABLE ONLY route_prod.re_entry_queue
    ADD CONSTRAINT fk_re_entry_queue_route FOREIGN KEY (route_id, current_version) REFERENCES route_prod.routes(route_id, version) ON DELETE CASCADE DEFERRABLE;


--
-- Name: routes fk_route_prod_service_route; Type: FK CONSTRAINT; Schema: route_prod; Owner: -
--

ALTER TABLE ONLY route_prod.routes
    ADD CONSTRAINT fk_route_prod_service_route FOREIGN KEY (service_route_id) REFERENCES route_raw.service_routes(service_route_id) ON DELETE SET NULL;


--
-- Name: route_semantics fk_route_semantics_service_route; Type: FK CONSTRAINT; Schema: route_prod; Owner: -
--

ALTER TABLE ONLY route_prod.route_semantics
    ADD CONSTRAINT fk_route_semantics_service_route FOREIGN KEY (service_route_id) REFERENCES route_raw.service_routes(service_route_id) ON DELETE SET NULL;


--
-- Name: routes fk_routes_chosen_geom; Type: FK CONSTRAINT; Schema: route_prod; Owner: -
--

ALTER TABLE ONLY route_prod.routes
    ADD CONSTRAINT fk_routes_chosen_geom FOREIGN KEY (chosen_geometry_candidate_id) REFERENCES route_work.geometry_candidates(geometry_candidate_id) ON DELETE RESTRICT;


--
-- Name: routes fk_routes_chosen_stop_sequence; Type: FK CONSTRAINT; Schema: route_prod; Owner: -
--

ALTER TABLE ONLY route_prod.routes
    ADD CONSTRAINT fk_routes_chosen_stop_sequence FOREIGN KEY (chosen_stop_sequence_candidate_id) REFERENCES route_work.stop_sequence_candidates(candidate_id) ON DELETE SET NULL;


--
-- Name: route_semantics route_semantics_route_id_fkey; Type: FK CONSTRAINT; Schema: route_prod; Owner: -
--

ALTER TABLE ONLY route_prod.route_semantics
    ADD CONSTRAINT route_semantics_route_id_fkey FOREIGN KEY (route_id) REFERENCES route_prod.routes(route_id) ON DELETE CASCADE;


--
-- Name: routes_audit routes_audit_approved_by_fkey; Type: FK CONSTRAINT; Schema: route_prod; Owner: -
--

ALTER TABLE ONLY route_prod.routes_audit
    ADD CONSTRAINT routes_audit_approved_by_fkey FOREIGN KEY (approved_by) REFERENCES console.users(user_id) ON DELETE SET NULL;


--
-- Name: routes routes_route_id_fkey; Type: FK CONSTRAINT; Schema: route_prod; Owner: -
--

ALTER TABLE ONLY route_prod.routes
    ADD CONSTRAINT routes_route_id_fkey FOREIGN KEY (route_id) REFERENCES route_raw.route_jobs(route_id) ON DELETE RESTRICT;


--
-- Name: route_jobs fk_route_jobs_service_route; Type: FK CONSTRAINT; Schema: route_raw; Owner: -
--

ALTER TABLE ONLY route_raw.route_jobs
    ADD CONSTRAINT fk_route_jobs_service_route FOREIGN KEY (service_route_id) REFERENCES route_raw.service_routes(service_route_id) ON DELETE SET NULL;


--
-- Name: osm_relations_raw osm_relations_raw_route_id_fkey; Type: FK CONSTRAINT; Schema: route_raw; Owner: -
--

ALTER TABLE ONLY route_raw.osm_relations_raw
    ADD CONSTRAINT osm_relations_raw_route_id_fkey FOREIGN KEY (route_id) REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE;


--
-- Name: relation_candidates relation_candidates_route_id_fkey; Type: FK CONSTRAINT; Schema: route_raw; Owner: -
--

ALTER TABLE ONLY route_raw.relation_candidates
    ADD CONSTRAINT relation_candidates_route_id_fkey FOREIGN KEY (route_id) REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE;


--
-- Name: service_route_directions service_route_directions_route_id_fkey; Type: FK CONSTRAINT; Schema: route_raw; Owner: -
--

ALTER TABLE ONLY route_raw.service_route_directions
    ADD CONSTRAINT service_route_directions_route_id_fkey FOREIGN KEY (route_id) REFERENCES route_raw.route_jobs(route_id) ON DELETE SET NULL;


--
-- Name: service_route_directions service_route_directions_service_route_id_fkey; Type: FK CONSTRAINT; Schema: route_raw; Owner: -
--

ALTER TABLE ONLY route_raw.service_route_directions
    ADD CONSTRAINT service_route_directions_service_route_id_fkey FOREIGN KEY (service_route_id) REFERENCES route_raw.service_routes(service_route_id) ON DELETE CASCADE;


--
-- Name: coverage_gaps coverage_gaps_resolved_prod_route_id_fkey; Type: FK CONSTRAINT; Schema: route_review; Owner: -
--

ALTER TABLE ONLY route_review.coverage_gaps
    ADD CONSTRAINT coverage_gaps_resolved_prod_route_id_fkey FOREIGN KEY (resolved_prod_route_id) REFERENCES route_prod.routes(route_id) ON DELETE SET NULL;


--
-- Name: coverage_gaps coverage_gaps_resolved_route_id_fkey; Type: FK CONSTRAINT; Schema: route_review; Owner: -
--

ALTER TABLE ONLY route_review.coverage_gaps
    ADD CONSTRAINT coverage_gaps_resolved_route_id_fkey FOREIGN KEY (resolved_route_id) REFERENCES route_raw.route_jobs(route_id) ON DELETE SET NULL;


--
-- Name: route_job_dedupe_groups route_job_dedupe_groups_canonical_route_id_fkey; Type: FK CONSTRAINT; Schema: route_review; Owner: -
--

ALTER TABLE ONLY route_review.route_job_dedupe_groups
    ADD CONSTRAINT route_job_dedupe_groups_canonical_route_id_fkey FOREIGN KEY (canonical_route_id) REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE;


--
-- Name: route_job_dedupe_memberships route_job_dedupe_memberships_canonical_route_id_fkey; Type: FK CONSTRAINT; Schema: route_review; Owner: -
--

ALTER TABLE ONLY route_review.route_job_dedupe_memberships
    ADD CONSTRAINT route_job_dedupe_memberships_canonical_route_id_fkey FOREIGN KEY (canonical_route_id) REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE;


--
-- Name: route_job_dedupe_memberships route_job_dedupe_memberships_dedupe_group_id_fkey; Type: FK CONSTRAINT; Schema: route_review; Owner: -
--

ALTER TABLE ONLY route_review.route_job_dedupe_memberships
    ADD CONSTRAINT route_job_dedupe_memberships_dedupe_group_id_fkey FOREIGN KEY (dedupe_group_id) REFERENCES route_review.route_job_dedupe_groups(dedupe_group_id) ON DELETE CASCADE;


--
-- Name: route_job_dedupe_memberships route_job_dedupe_memberships_route_id_fkey; Type: FK CONSTRAINT; Schema: route_review; Owner: -
--

ALTER TABLE ONLY route_review.route_job_dedupe_memberships
    ADD CONSTRAINT route_job_dedupe_memberships_route_id_fkey FOREIGN KEY (route_id) REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE;


--
-- Name: delete_events delete_events_trash_id_fkey; Type: FK CONSTRAINT; Schema: route_trash; Owner: -
--

ALTER TABLE ONLY route_trash.delete_events
    ADD CONSTRAINT delete_events_trash_id_fkey FOREIGN KEY (trash_id) REFERENCES route_trash.trash_items(trash_id);


--
-- Name: manual_sequence_exports fk_manual_sequence_exports_gap; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.manual_sequence_exports
    ADD CONSTRAINT fk_manual_sequence_exports_gap FOREIGN KEY (coverage_gap_id) REFERENCES route_review.coverage_gaps(gap_id) ON DELETE SET NULL;


--
-- Name: geometry_candidate_sets geometry_candidate_sets_route_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.geometry_candidate_sets
    ADD CONSTRAINT geometry_candidate_sets_route_id_fkey FOREIGN KEY (route_id) REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE;


--
-- Name: geometry_candidate_sets geometry_candidate_sets_stop_sequence_set_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.geometry_candidate_sets
    ADD CONSTRAINT geometry_candidate_sets_stop_sequence_set_id_fkey FOREIGN KEY (stop_sequence_set_id) REFERENCES route_work.stop_sequence_candidate_sets(set_id);


--
-- Name: geometry_candidates geometry_candidates_preset_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.geometry_candidates
    ADD CONSTRAINT geometry_candidates_preset_id_fkey FOREIGN KEY (preset_id) REFERENCES route_work.valhalla_presets(preset_id);


--
-- Name: geometry_candidates geometry_candidates_set_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.geometry_candidates
    ADD CONSTRAINT geometry_candidates_set_id_fkey FOREIGN KEY (set_id) REFERENCES route_work.geometry_candidate_sets(set_id) ON DELETE CASCADE;


--
-- Name: geometry_candidates geometry_candidates_stop_sequence_candidate_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.geometry_candidates
    ADD CONSTRAINT geometry_candidates_stop_sequence_candidate_id_fkey FOREIGN KEY (stop_sequence_candidate_id) REFERENCES route_work.stop_sequence_candidates(candidate_id) ON DELETE CASCADE;


--
-- Name: geometry_stop_recovery geometry_stop_recovery_geometry_candidate_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.geometry_stop_recovery
    ADD CONSTRAINT geometry_stop_recovery_geometry_candidate_id_fkey FOREIGN KEY (geometry_candidate_id) REFERENCES route_work.geometry_candidates(geometry_candidate_id) ON DELETE CASCADE;


--
-- Name: geometry_stop_recovery geometry_stop_recovery_route_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.geometry_stop_recovery
    ADD CONSTRAINT geometry_stop_recovery_route_id_fkey FOREIGN KEY (route_id) REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE;


--
-- Name: geometry_stop_recovery geometry_stop_recovery_set_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.geometry_stop_recovery
    ADD CONSTRAINT geometry_stop_recovery_set_id_fkey FOREIGN KEY (set_id) REFERENCES route_work.geometry_candidate_sets(set_id) ON DELETE CASCADE;


--
-- Name: geometry_stop_recovery geometry_stop_recovery_stop_sequence_candidate_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.geometry_stop_recovery
    ADD CONSTRAINT geometry_stop_recovery_stop_sequence_candidate_id_fkey FOREIGN KEY (stop_sequence_candidate_id) REFERENCES route_work.stop_sequence_candidates(candidate_id) ON DELETE SET NULL;


--
-- Name: inverse_direction_status inverse_direction_status_anchor_route_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.inverse_direction_status
    ADD CONSTRAINT inverse_direction_status_anchor_route_id_fkey FOREIGN KEY (anchor_route_id) REFERENCES route_raw.route_jobs(route_id) ON DELETE SET NULL;


--
-- Name: inverse_direction_status inverse_direction_status_bound_route_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.inverse_direction_status
    ADD CONSTRAINT inverse_direction_status_bound_route_id_fkey FOREIGN KEY (bound_route_id) REFERENCES route_raw.route_jobs(route_id) ON DELETE SET NULL;


--
-- Name: inverse_direction_status inverse_direction_status_service_route_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.inverse_direction_status
    ADD CONSTRAINT inverse_direction_status_service_route_id_fkey FOREIGN KEY (service_route_id) REFERENCES route_raw.service_routes(service_route_id) ON DELETE CASCADE;


--
-- Name: inverse_direction_status inverse_direction_status_top_candidate_route_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.inverse_direction_status
    ADD CONSTRAINT inverse_direction_status_top_candidate_route_id_fkey FOREIGN KEY (top_candidate_route_id) REFERENCES route_raw.route_jobs(route_id) ON DELETE SET NULL;


--
-- Name: manual_sequence_drafts manual_sequence_drafts_route_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.manual_sequence_drafts
    ADD CONSTRAINT manual_sequence_drafts_route_id_fkey FOREIGN KEY (route_id) REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE;


--
-- Name: manual_sequence_drafts manual_sequence_drafts_service_route_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.manual_sequence_drafts
    ADD CONSTRAINT manual_sequence_drafts_service_route_id_fkey FOREIGN KEY (service_route_id) REFERENCES route_raw.service_routes(service_route_id) ON DELETE SET NULL;


--
-- Name: manual_sequence_exports manual_sequence_exports_draft_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.manual_sequence_exports
    ADD CONSTRAINT manual_sequence_exports_draft_id_fkey FOREIGN KEY (draft_id) REFERENCES route_work.manual_sequence_drafts(draft_id) ON DELETE SET NULL;


--
-- Name: manual_sequence_exports manual_sequence_exports_route_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.manual_sequence_exports
    ADD CONSTRAINT manual_sequence_exports_route_id_fkey FOREIGN KEY (route_id) REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE;


--
-- Name: manual_sequence_exports manual_sequence_exports_service_route_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.manual_sequence_exports
    ADD CONSTRAINT manual_sequence_exports_service_route_id_fkey FOREIGN KEY (service_route_id) REFERENCES route_raw.service_routes(service_route_id) ON DELETE SET NULL;


--
-- Name: manual_sequence_exports manual_sequence_exports_stop_sequence_candidate_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.manual_sequence_exports
    ADD CONSTRAINT manual_sequence_exports_stop_sequence_candidate_id_fkey FOREIGN KEY (stop_sequence_candidate_id) REFERENCES route_work.stop_sequence_candidates(candidate_id) ON DELETE SET NULL;


--
-- Name: manual_sequence_exports manual_sequence_exports_stop_sequence_set_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.manual_sequence_exports
    ADD CONSTRAINT manual_sequence_exports_stop_sequence_set_id_fkey FOREIGN KEY (stop_sequence_set_id) REFERENCES route_work.stop_sequence_candidate_sets(set_id) ON DELETE SET NULL;


--
-- Name: relation_stop_prior relation_stop_prior_route_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.relation_stop_prior
    ADD CONSTRAINT relation_stop_prior_route_id_fkey FOREIGN KEY (route_id) REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE;


--
-- Name: route_approvals route_approvals_chosen_geometry_candidate_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.route_approvals
    ADD CONSTRAINT route_approvals_chosen_geometry_candidate_id_fkey FOREIGN KEY (chosen_geometry_candidate_id) REFERENCES route_work.geometry_candidates(geometry_candidate_id);


--
-- Name: route_approvals route_approvals_chosen_stop_sequence_candidate_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.route_approvals
    ADD CONSTRAINT route_approvals_chosen_stop_sequence_candidate_id_fkey FOREIGN KEY (chosen_stop_sequence_candidate_id) REFERENCES route_work.stop_sequence_candidates(candidate_id);


--
-- Name: route_approvals route_approvals_route_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.route_approvals
    ADD CONSTRAINT route_approvals_route_id_fkey FOREIGN KEY (route_id) REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE;


--
-- Name: sequence_approvals sequence_approvals_chosen_stop_sequence_candidate_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.sequence_approvals
    ADD CONSTRAINT sequence_approvals_chosen_stop_sequence_candidate_id_fkey FOREIGN KEY (chosen_stop_sequence_candidate_id) REFERENCES route_work.stop_sequence_candidates(candidate_id) ON DELETE SET NULL;


--
-- Name: sequence_approvals sequence_approvals_route_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.sequence_approvals
    ADD CONSTRAINT sequence_approvals_route_id_fkey FOREIGN KEY (route_id) REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE;


--
-- Name: sequence_approvals sequence_approvals_stop_sequence_set_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.sequence_approvals
    ADD CONSTRAINT sequence_approvals_stop_sequence_set_id_fkey FOREIGN KEY (stop_sequence_set_id) REFERENCES route_work.stop_sequence_candidate_sets(set_id) ON DELETE SET NULL;


--
-- Name: service_route_approvals service_route_approvals_service_route_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.service_route_approvals
    ADD CONSTRAINT service_route_approvals_service_route_id_fkey FOREIGN KEY (service_route_id) REFERENCES route_raw.service_routes(service_route_id) ON DELETE CASCADE;


--
-- Name: stop_sequence_candidate_sets stop_sequence_candidate_sets_route_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.stop_sequence_candidate_sets
    ADD CONSTRAINT stop_sequence_candidate_sets_route_id_fkey FOREIGN KEY (route_id) REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE;


--
-- Name: stop_sequence_candidates stop_sequence_candidates_set_id_fkey; Type: FK CONSTRAINT; Schema: route_work; Owner: -
--

ALTER TABLE ONLY route_work.stop_sequence_candidates
    ADD CONSTRAINT stop_sequence_candidates_set_id_fkey FOREIGN KEY (set_id) REFERENCES route_work.stop_sequence_candidate_sets(set_id) ON DELETE CASCADE;


--
-- Name: route_evidence_records fk_rer_route_hint; Type: FK CONSTRAINT; Schema: semantics; Owner: -
--

ALTER TABLE ONLY semantics.route_evidence_records
    ADD CONSTRAINT fk_rer_route_hint FOREIGN KEY (route_id_hint) REFERENCES route_raw.route_jobs(route_id) ON DELETE SET NULL;


--
-- Name: route_name_candidates route_name_candidates_route_id_fkey; Type: FK CONSTRAINT; Schema: semantics; Owner: -
--

ALTER TABLE ONLY semantics.route_name_candidates
    ADD CONSTRAINT route_name_candidates_route_id_fkey FOREIGN KEY (route_id) REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE;


--
-- Name: route_name_feedback route_name_feedback_candidate_id_fkey; Type: FK CONSTRAINT; Schema: semantics; Owner: -
--

ALTER TABLE ONLY semantics.route_name_feedback
    ADD CONSTRAINT route_name_feedback_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES semantics.route_name_candidates(candidate_id) ON DELETE CASCADE;


--
-- Name: route_name_feedback route_name_feedback_route_id_fkey; Type: FK CONSTRAINT; Schema: semantics; Owner: -
--

ALTER TABLE ONLY semantics.route_name_feedback
    ADD CONSTRAINT route_name_feedback_route_id_fkey FOREIGN KEY (route_id) REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE;


--
-- Name: route_name_seed_runs route_name_seed_runs_route_id_fkey; Type: FK CONSTRAINT; Schema: semantics; Owner: -
--

ALTER TABLE ONLY semantics.route_name_seed_runs
    ADD CONSTRAINT route_name_seed_runs_route_id_fkey FOREIGN KEY (route_id) REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--


