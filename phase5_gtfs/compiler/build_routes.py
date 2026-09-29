from __future__ import annotations

import os
from typing import Dict, Any, Optional

from datamind_console.common.name_normalizer import normalize_name
from phase5_gtfs.common.config import db_conn
from phase5_gtfs.errors import assert_no_null_direction_ids, assert_no_unapproved_semantics


def build_routes_and_stops(
    export_run_id: str,
    *,
    route_id: Optional[str] = None,
    include_stops: bool = True,
) -> Dict[str, Any]:
    # Phase 5 contract: refuse to compile routes if any GTFS-eligible route
    # is missing a direction_id (i.e. Phase 4.5 hasn't run or left routes
    # in operator_pending). See workspace/skills/direction_construction.md.
    # operator_pending routes are intentionally NULL (Phase 4.5 contract)
    # and excluded from this gate; the compiler filters them out below.
    province_scope = os.getenv("PHASE5_PROVINCE_SCOPE") or None
    assert_no_null_direction_ids(province=province_scope)

    # Phase 4 contract: refuse to compile routes whose catalog.route_semantics
    # row was never operator-approved (and aren't legacy_grandfathered).
    assert_no_unapproved_semantics(province=province_scope)

    with db_conn() as conn:
        with conn.cursor() as cur:
            route_id_txt = str(route_id or "").strip()
            if route_id_txt:
                cur.execute(
                    """
                    DELETE FROM gtfs_work.gtfs_routes
                    WHERE export_run_id = %s::uuid
                      AND route_id IN (
                        SELECT COALESCE(r.service_route_id::text, r.route_id::text)
                        FROM route_prod.routes r
                        WHERE r.route_id::text = %s
                      )
                    """,
                    (export_run_id, route_id_txt),
                )
                if include_stops:
                    # Scoped stop rebuild mode: only clear stops referenced by this route.
                    cur.execute(
                        """
                        DELETE FROM gtfs_work.gtfs_stops s
                        WHERE s.export_run_id = %s::uuid
                          AND s.stop_id IN (
                            SELECT DISTINCT n.node_id::text
                            FROM route_prod.routes r
                            JOIN LATERAL unnest(r.stop_node_ids) AS sid(node_id) ON true
                            JOIN node_prod.nodes n ON n.node_id = sid.node_id
                            WHERE r.route_id::text = %s
                          )
                        """,
                        (export_run_id, route_id_txt),
                    )
                # Do not wipe all agencies during scoped route rebuilds; they are upserted below.
            else:
                cur.execute("DELETE FROM gtfs_work.gtfs_routes WHERE export_run_id = %s::uuid", (export_run_id,))
                if include_stops:
                    cur.execute("DELETE FROM gtfs_work.gtfs_stops WHERE export_run_id = %s::uuid", (export_run_id,))
                cur.execute("DELETE FROM gtfs_work.gtfs_agency WHERE export_run_id = %s::uuid", (export_run_id,))

            where = ""
            scope_params = []
            if route_id_txt:
                where = "WHERE v.route_id = %s"
                scope_params.append(route_id_txt)

            # default fallback agency (used only when neither linked agency nor operator_name is available)
            cur.execute(
                """
                INSERT INTO gtfs_work.gtfs_agency (export_run_id, agency_id, agency_name, agency_url, agency_timezone, agency_lang)
                VALUES (%s::uuid, 'datamind', 'DataMind Transit', 'https://datamind.local', 'America/Guayaquil', 'es')
                ON CONFLICT (export_run_id, agency_id) DO NOTHING
                """,
                (export_run_id,),
            )

            # agencies actually used by selected routes (from normalized links)
            cur.execute(
                f"""
                WITH route_scope AS (
                  SELECT
                    v.route_id::uuid AS route_id,
                    COALESCE(
                      NULLIF(BTRIM(v.operator_name), ''),
                      NULLIF(BTRIM(sr.operator_name), ''),
                      ''
                    )::text AS operator_name
                  FROM gtfs_work.v_route_inputs v
                  LEFT JOIN route_prod.routes rr ON rr.route_id = v.route_id::uuid
                  LEFT JOIN route_raw.service_routes sr ON sr.service_route_id = rr.service_route_id
                  {where}
                ),
                used AS (
                  SELECT
                    rs.route_id,
                    CASE
                      WHEN l.status = 'linked' AND COALESCE(NULLIF(BTRIM(l.agency_id), ''), '') <> '' THEN l.agency_id
                      WHEN rs.operator_name <> '' THEN
                        ('op_' || regexp_replace(lower(rs.operator_name), '[^a-z0-9]+', '_', 'g'))
                      ELSE 'datamind'
                    END::text AS agency_id,
                    CASE
                      WHEN l.status = 'linked' THEN NULL::text
                      WHEN rs.operator_name <> '' THEN rs.operator_name
                      ELSE 'DataMind Transit'
                    END::text AS agency_name_fallback
                  FROM route_scope rs
                  LEFT JOIN gtfs_work.route_agency_links l
                    ON l.route_id = rs.route_id
                )
                INSERT INTO gtfs_work.gtfs_agency (
                  export_run_id, agency_id, agency_name, agency_url, agency_timezone, agency_lang
                )
                SELECT
                  %s::uuid AS export_run_id,
                  deduped.agency_id,
                  deduped.agency_name,
                  deduped.agency_url,
                  deduped.agency_timezone,
                  deduped.agency_lang
                FROM (
                  SELECT DISTINCT ON (u.agency_id)
                    u.agency_id,
                    COALESCE(c.agency_name, u.agency_name_fallback, 'DataMind Transit') AS agency_name,
                    COALESCE(c.agency_url, 'https://datamind.local') AS agency_url,
                    COALESCE(c.agency_timezone, 'America/Guayaquil') AS agency_timezone,
                    COALESCE(c.agency_lang, 'es') AS agency_lang
                  FROM used u
                  LEFT JOIN gtfs_work.agency_catalog c
                    ON c.agency_id = u.agency_id
                  ORDER BY u.agency_id
                ) deduped
                ON CONFLICT (export_run_id, agency_id) DO UPDATE SET
                  agency_name = EXCLUDED.agency_name,
                  agency_url = EXCLUDED.agency_url,
                  agency_timezone = EXCLUDED.agency_timezone,
                  agency_lang = EXCLUDED.agency_lang
                """,
                tuple(scope_params + [export_run_id]),
            )

            # routes
            cur.execute(
                f"""
                WITH route_scope AS (
                  SELECT
                    r.route_id::text AS source_route_id,
                    COALESCE(r.service_route_id::text, r.route_id::text) AS gtfs_route_id,
                    COALESCE(v.route_ref, NULL) AS route_ref,
                    COALESCE(v.route_name, r.route_name, 'route_' || LEFT(r.route_id::text, 8)) AS route_name,
                    COALESCE(
                      NULLIF(BTRIM(v.operator_name), ''),
                      NULLIF(BTRIM(sr.operator_name), ''),
                      NULL
                    ) AS operator_name,
                    r.direction_id::int AS direction_id
                  FROM route_prod.routes r
                  LEFT JOIN gtfs_work.v_route_inputs v
                    ON v.route_id = r.route_id
                  LEFT JOIN route_raw.service_routes sr
                    ON sr.service_route_id = r.service_route_id
                  WHERE COALESCE(r.canonical_sequence_ready, FALSE) = TRUE
                    AND r.chosen_stop_sequence_candidate_id IS NOT NULL
                    AND EXISTS (
                        SELECT 1 FROM gtfs_work.route_schedule_profiles sp
                        WHERE sp.route_id = r.route_id AND sp.is_active = true
                    )
                  {('AND r.route_id::text = %s' if route_id_txt else '')}
                ),
                ranked AS (
                  SELECT
                    rs.*,
                    l.status AS link_status,
                    l.agency_id AS linked_agency_id,
                    ROW_NUMBER() OVER (
                      PARTITION BY rs.gtfs_route_id
                      ORDER BY
                        CASE WHEN rs.source_route_id = rs.gtfs_route_id THEN 0 ELSE 1 END,
                        CASE WHEN rs.direction_id = 0 THEN 0 ELSE 1 END,
                        rs.source_route_id
                    ) AS rn
                  FROM route_scope rs
                  LEFT JOIN gtfs_work.route_agency_links l
                    ON l.route_id::text = rs.source_route_id
                )
                INSERT INTO gtfs_work.gtfs_routes (
                  export_run_id, route_id, agency_id, route_short_name, route_long_name, route_type, route_color, route_text_color
                )
                SELECT
                  %s::uuid,
                  x.gtfs_route_id,
                  CASE
                    WHEN x.link_status = 'linked' AND COALESCE(NULLIF(BTRIM(x.linked_agency_id), ''), '') <> '' THEN x.linked_agency_id
                    WHEN COALESCE(NULLIF(BTRIM(x.operator_name), ''), '') <> '' THEN
                      ('op_' || regexp_replace(lower(BTRIM(x.operator_name)), '[^a-z0-9]+', '_', 'g'))
                    ELSE 'datamind'
                  END::text AS agency_id,
                  COALESCE(x.route_ref, LEFT(x.gtfs_route_id, 6)),
                  x.route_name,
                  3,
                  '1F77B4',
                  'FFFFFF'
                FROM ranked x
                WHERE x.rn = 1
                  AND (
                    (x.link_status = 'linked' AND COALESCE(NULLIF(BTRIM(x.linked_agency_id), ''), '') <> '')
                    OR COALESCE(NULLIF(BTRIM(x.operator_name), ''), '') <> ''
                  )
                  AND COALESCE(x.operator_name, '') !~* '^\[pending research\]|^unknown|^desconocido|^unnamed'
                ON CONFLICT (export_run_id, route_id) DO UPDATE SET
                  agency_id = EXCLUDED.agency_id,
                  route_short_name = EXCLUDED.route_short_name,
                  route_long_name = EXCLUDED.route_long_name
                """,
                tuple(([route_id_txt] if route_id_txt else []) + [export_run_id]),
            )
            n_routes = int(cur.rowcount or 0)

            n_stops = 0
            if include_stops:
                # stops from node_prod (legacy mode); preferred flow uses dedicated Phase 2 canonical stops step.
                if route_id:
                    stop_where = "WHERE r.route_id = %s"
                    stop_params = (export_run_id, route_id)
                else:
                    stop_where = ""
                    stop_params = (export_run_id,)

                cur.execute(
                    f"""
                    INSERT INTO gtfs_work.gtfs_stops (
                      export_run_id, stop_id, stop_name, stop_lat, stop_lon, location_type, parent_station
                    )
                    SELECT DISTINCT
                      %s::uuid,
                      n.node_id::text,
                      COALESCE(NULLIF(n.name, ''), 'Stop ' || LEFT(n.node_id::text, 8)),
                      ST_Y(n.geom),
                      ST_X(n.geom),
                      0,
                      NULL
                    FROM route_prod.routes r
                    JOIN LATERAL unnest(r.stop_node_ids) AS sid(node_id) ON true
                    JOIN node_prod.nodes n ON n.node_id = sid.node_id
                    {stop_where}
                    {('AND' if stop_where else 'WHERE')} COALESCE(r.canonical_sequence_ready, FALSE) = TRUE
                    AND r.chosen_stop_sequence_candidate_id IS NOT NULL
                    AND EXISTS (
                        SELECT 1 FROM gtfs_work.route_schedule_profiles sp
                        WHERE sp.route_id = r.route_id AND sp.is_active = true
                    )
                    ON CONFLICT (export_run_id, stop_id) DO UPDATE SET
                      stop_name = EXCLUDED.stop_name,
                      stop_lat = EXCLUDED.stop_lat,
                      stop_lon = EXCLUDED.stop_lon
                    """,
                    stop_params,
                )
                n_stops = int(cur.rowcount or 0)

            # Normalize stop and route names
            cur.execute(
                "SELECT stop_id, stop_name FROM gtfs_work.gtfs_stops WHERE export_run_id = %s::uuid",
                (export_run_id,),
            )
            for row in cur.fetchall():
                sid, sname = (row[0], row[1]) if not isinstance(row, dict) else (row["stop_id"], row["stop_name"])
                if sname:
                    normed = normalize_name(sname)
                    if normed and normed != sname:
                        cur.execute(
                            "UPDATE gtfs_work.gtfs_stops SET stop_name = %s WHERE export_run_id = %s::uuid AND stop_id = %s",
                            (normed, export_run_id, sid),
                        )
            cur.execute(
                "SELECT route_id, route_long_name FROM gtfs_work.gtfs_routes WHERE export_run_id = %s::uuid",
                (export_run_id,),
            )
            for row in cur.fetchall():
                rid, rname = (row[0], row[1]) if not isinstance(row, dict) else (row["route_id"], row["route_long_name"])
                if rname:
                    normed = normalize_name(rname)
                    if normed and normed != rname:
                        cur.execute(
                            "UPDATE gtfs_work.gtfs_routes SET route_long_name = %s WHERE export_run_id = %s::uuid AND route_id = %s",
                            (normed, export_run_id, rid),
                        )

            # Normalize agency names
            cur.execute(
                "SELECT agency_id, agency_name FROM gtfs_work.gtfs_agency WHERE export_run_id = %s::uuid",
                (export_run_id,),
            )
            for row in cur.fetchall():
                aid, aname = (row[0], row[1]) if not isinstance(row, dict) else (row["agency_id"], row["agency_name"])
                if aname:
                    normed = normalize_name(aname)
                    if normed and normed != aname:
                        cur.execute(
                            "UPDATE gtfs_work.gtfs_agency SET agency_name = %s WHERE export_run_id = %s::uuid AND agency_id = %s",
                            (normed, export_run_id, aid),
                        )

            # Clean up orphan agencies that no route references
            cur.execute(
                """
                DELETE FROM gtfs_work.gtfs_agency a
                WHERE a.export_run_id = %s::uuid
                  AND NOT EXISTS (
                      SELECT 1 FROM gtfs_work.gtfs_routes r
                      WHERE r.export_run_id = a.export_run_id
                        AND r.agency_id = a.agency_id
                  )
                """,
                (export_run_id,),
            )

    return {"routes": n_routes, "stops": n_stops}
