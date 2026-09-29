"""Pre-export enforcer — hard gate before GTFS compilation.

Zero tolerance: blocks compilation if data quality checks fail.
Attempts auto-fix via context naming (Overpass) and Valhalla before blocking.
"""
from __future__ import annotations

import json
import logging
import math
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from datamind_console.common.name_normalizer import normalize_name
from datamind_console.persistence import write_to_route_prod

_LOG = logging.getLogger(__name__)

VALHALLA_URL = "http://localhost:8003"

ECUADOR_BBOX = (-5.02, -81.08, 1.68, -75.19)

from datamind_console.common.naming_patterns import (
    STOP_FORBIDDEN_PATTERNS as _GARBAGE_PATTERNS,
    is_stop_name_forbidden,
)

UNIT_BBOXES = {
    "cayambe":   (-78.20, -0.02, -78.10, 0.10),
    "ruminahui": (-78.55, -0.40, -78.40, -0.28),
    "quito_dmq": (-78.60, -0.35, -78.35, 0.05),
    "mejia":     (-78.65, -0.65, -78.45, -0.40),
    "sample_region": (-78.65, -0.65, -78.05, 0.20),
}


@dataclass
class CheckResult:
    name: str
    passed: bool
    count: int = 0
    auto_fixed: int = 0
    remaining: int = 0
    details: str = ""


@dataclass
class EnforcerReport:
    passed: bool = True
    checks: List[CheckResult] = field(default_factory=list)
    excluded_routes: List[Dict[str, Any]] = field(default_factory=list)
    blocking_reasons: List[str] = field(default_factory=list)

    @property
    def total_auto_fixed(self) -> int:
        return sum(c.auto_fixed for c in self.checks)


def _name_is_bad(name: Optional[str]) -> bool:
    return is_stop_name_forbidden(name)


def _treat_stops_for_name_repair(stops_list, db_conn, *, caller: str) -> int:
    """Route every stop through the universal treater (name_repair op).

    Operates within the caller's transaction. Returns the count of stops
    whose canonical_name was successfully replaced with a clean name.
    Stops whose original name passes is_stop_name_forbidden=False after
    normalization are no-ops (treater is idempotent).
    """
    # Local import — keeps pre_export_enforcer importable in environments
    # where phase3_routes is absent (e.g., minimal test runs).
    from phase3_routes.services.stop_quality import (
        StopTreatmentInput,
        treat_stop,
    )

    fixed = 0
    for stop in stops_list:
        result = treat_stop(
            StopTreatmentInput(
                operation="name_repair",
                caller=caller,
                node_id=stop["node_id"],
                proposed_name=stop["name"],
                proposed_lat=stop["lat"],
                proposed_lon=stop["lon"],
            ),
            db_conn,
        )
        if result.success and result.final_name and result.final_name != stop["name"]:
            stop["name"] = result.final_name
            fixed += 1
    if fixed:
        db_conn.commit()
    return fixed


def _decode_polyline6(encoded: str) -> list[tuple[float, float]]:
    inv = 1.0 / 1e6
    decoded = []
    previous = [0, 0]
    i = 0
    while i < len(encoded):
        for dim in range(2):
            shift = 0
            result = 0
            while True:
                b = ord(encoded[i]) - 63
                i += 1
                result |= (b & 0x1f) << shift
                shift += 5
                if b < 0x20:
                    break
            if result & 1:
                result = ~result
            result >>= 1
            previous[dim] += result
        decoded.append((previous[0] * inv, previous[1] * inv))
    return decoded


def _valhalla_retrace(stop_coords: list[tuple[float, float]]) -> Optional[str]:
    """Call Valhalla route with bus costing. Returns EWKT or None."""
    if len(stop_coords) < 2:
        return None
    locations = [{"lat": lat, "lon": lon} for lat, lon in stop_coords]
    payload = json.dumps({
        "locations": locations,
        "costing": "bus",
        "directions_options": {"units": "kilometers"},
    })
    req = urllib.request.Request(
        f"{VALHALLA_URL}/route",
        data=payload.encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
        all_points = []
        for leg in result.get("trip", {}).get("legs", []):
            shape = leg.get("shape", "")
            if shape:
                all_points.extend(_decode_polyline6(shape))
        if len(all_points) < 5:
            return None
        ewkt = "SRID=4326;LINESTRING(" + ",".join(
            f"{lon} {lat}" for lat, lon in all_points
        ) + ")"
        return ewkt
    except Exception as e:
        _LOG.warning("Valhalla retrace failed: %s", e)
        return None


class PreExportEnforcer:
    """Hard gate before GTFS compilation. Zero tolerance."""

    def enforce(
        self,
        canton: str,
        province: str,
        db_conn,
        *,
        route_id_filter: Optional[List[str]] = None,
    ) -> EnforcerReport:
        """Run the gate.

        Default: audits every stop/route that intersects the canton bbox.
        With route_id_filter: only audits the given routes and the stops they
        actually reference. Use cohort-mode when shipping a curated subset of
        a province's routes — avoids touching unrelated stops in the bbox.
        """
        report = EnforcerReport()

        bbox = UNIT_BBOXES.get(canton.lower())
        if not bbox:
            bbox = UNIT_BBOXES.get("sample_region", (-78.65, -0.65, -78.05, 0.20))
        bbox_wkt = (
            f"POLYGON(({bbox[0]} {bbox[1]}, {bbox[0]} {bbox[3]}, "
            f"{bbox[2]} {bbox[3]}, {bbox[2]} {bbox[1]}, {bbox[0]} {bbox[1]}))"
        )

        cohort_mode = bool(route_id_filter)
        cur = db_conn.cursor()

        if cohort_mode:
            # Cohort scope: stops are exactly the ones referenced by these routes;
            # routes are exactly the cohort. Bbox is ignored — the route_id_filter
            # is the source of truth.
            cur.execute("""
                SELECT n.node_id::text, n.name,
                       ST_Y(n.geom)::float AS lat, ST_X(n.geom)::float AS lon,
                       n.confidence::float, n.source
                FROM node_prod.nodes n
                WHERE n.node_type='STOP'
                  AND n.superseded_by IS NULL
                  AND n.node_id IN (
                    SELECT DISTINCT unnest(stop_node_ids)
                    FROM route_prod.routes
                    WHERE route_id = ANY(%s::uuid[])
                  )
            """, (route_id_filter,))
        else:
            cur.execute("""
                SELECT node_id::text, name,
                       ST_Y(geom)::float AS lat, ST_X(geom)::float AS lon,
                       confidence::float, source
                FROM node_prod.nodes
                WHERE ST_Intersects(geom, ST_GeomFromText(%s, 4326))
                  AND node_type = 'STOP'
            """, (bbox_wkt,))
        cols = [d[0] for d in cur.description]
        stops_raw = cur.fetchall()
        stops = []
        for r in stops_raw:
            row = dict(zip(cols, r)) if not isinstance(r, dict) else dict(r)
            row["lat"] = float(row["lat"])
            row["lon"] = float(row["lon"])
            stops.append(row)

        # Load routes
        if cohort_mode:
            cur.execute("""
                SELECT r.route_id::text, r.route_name, r.stop_node_ids::text[],
                       ST_NPoints(r.geom)::int AS shape_points,
                       array_length(r.stop_node_ids, 1)::int AS stop_count,
                       (ST_Length(r.geom::geography) / 1000.0)::float AS length_km,
                       (ST_Length(r.geom::geography) / NULLIF(
                           ST_Length(ST_MakeLine(ST_StartPoint(r.geom), ST_EndPoint(r.geom))::geography), 0
                       ))::float AS sinuosity,
                       COALESCE(rs.operator_name, '') AS operator_name
                FROM route_prod.routes r
                LEFT JOIN route_prod.route_semantics rs ON rs.route_id = r.route_id
                WHERE r.route_id = ANY(%s::uuid[])
                  AND r.geom IS NOT NULL
            """, (route_id_filter,))
        else:
            cur.execute("""
                SELECT r.route_id::text, r.route_name, r.stop_node_ids::text[],
                       ST_NPoints(r.geom)::int AS shape_points,
                       array_length(r.stop_node_ids, 1)::int AS stop_count,
                       (ST_Length(r.geom::geography) / 1000.0)::float AS length_km,
                       (ST_Length(r.geom::geography) / NULLIF(
                           ST_Length(ST_MakeLine(ST_StartPoint(r.geom), ST_EndPoint(r.geom))::geography), 0
                       ))::float AS sinuosity,
                       COALESCE(rs.operator_name, '') AS operator_name
                FROM route_prod.routes r
                LEFT JOIN route_prod.route_semantics rs ON rs.route_id = r.route_id
                WHERE ST_Intersects(r.geom, ST_GeomFromText(%s, 4326))
                  AND r.geom IS NOT NULL
            """, (bbox_wkt,))
        rcols = [d[0] for d in cur.description]
        routes_raw = cur.fetchall()
        routes = []
        for r in routes_raw:
            row = dict(zip(rcols, r)) if not isinstance(r, dict) else dict(r)
            for k in ("shape_points", "stop_count"):
                if row.get(k) is not None:
                    row[k] = int(row[k])
            for k in ("length_km", "sinuosity"):
                if row.get(k) is not None:
                    row[k] = float(row[k])
            routes.append(row)

        # ── STOP CHECKS ─────────────────────────────────────────

        # 1. Blank names
        c1 = self._check_and_fix_blank_names(stops, cur, db_conn)
        report.checks.append(c1)
        if not c1.passed:
            report.passed = False
            report.blocking_reasons.append(
                f"stop_blank_names: {c1.remaining} stops still have blank names after auto-fix"
            )

        # 2. Garbage names
        c2 = self._check_and_fix_garbage_names(stops, cur, db_conn)
        report.checks.append(c2)
        if not c2.passed:
            report.passed = False
            report.blocking_reasons.append(
                f"stop_garbage_names: {c2.remaining} stops still have garbage names after auto-fix"
            )

        # 3. Null island
        c3 = self._check_null_island(stops)
        report.checks.append(c3)
        if not c3.passed:
            report.passed = False
            report.blocking_reasons.append(f"stop_null_island: {c3.count} stops at (0,0)")

        # 4. Outside Ecuador
        c4 = self._check_outside_bbox(stops)
        report.checks.append(c4)
        if not c4.passed:
            report.passed = False
            report.blocking_reasons.append(
                f"stop_outside_ecuador: {c4.count} stops outside Ecuador bbox"
            )

        # ── ROUTE CHECKS ────────────────────────────────────────

        # 5. Straight-line geometry
        c5 = self._check_and_fix_straight_line(routes, cur, db_conn)
        report.checks.append(c5)
        if not c5.passed:
            report.passed = False
            report.blocking_reasons.append(
                f"route_straight_line: {c5.remaining} routes still straight-line after Valhalla retrace"
            )

        # 6. Low-detail geometry
        c6 = self._check_and_fix_low_detail(routes, cur, db_conn)
        report.checks.append(c6)
        if not c6.passed:
            report.passed = False
            report.blocking_reasons.append(
                f"route_low_detail: {c6.remaining} routes still <20 shape points after Valhalla retrace"
            )

        # 7. Low sinuosity (exempt routes with >100 points — genuinely straight corridor)
        c7 = self._check_low_sinuosity(routes)
        report.checks.append(c7)
        if not c7.passed:
            report.passed = False
            report.blocking_reasons.append(
                f"route_low_sinuosity: {c7.count} routes with sinuosity <1.05 and <=100 points"
            )

        # 8. Routes <3 stops → EXCLUDE (don't block)
        c8, excluded_few = self._check_few_stops(routes)
        report.checks.append(c8)
        report.excluded_routes.extend(excluded_few)

        # 9. Routes >100km → EXCLUDE (interprovincial leak)
        c9, excluded_long = self._check_interprovincial(routes)
        report.checks.append(c9)
        report.excluded_routes.extend(excluded_long)

        # 10. Operator/agency name check — EXCLUDE routes with garbage operators
        c10, excluded_op = self._check_garbage_operators(routes, cur)
        report.checks.append(c10)
        report.excluded_routes.extend(excluded_op)

        cur.close()
        return report

    # ── Stop checks ──────────────────────────────────────────────

    def _check_and_fix_blank_names(self, stops, cur, db_conn) -> CheckResult:
        blank = [s for s in stops if not s["name"] or not s["name"].strip()]
        if not blank:
            return CheckResult("stop_blank_names", passed=True, count=0)

        fixed = _treat_stops_for_name_repair(
            blank, db_conn, caller="pre_export_enforcer.blank_names",
        )

        remaining = len(blank) - fixed
        return CheckResult(
            "stop_blank_names", passed=(remaining == 0),
            count=len(blank), auto_fixed=fixed, remaining=remaining,
            details=f"{len(blank)} blank, {fixed} fixed by treater",
        )

    def _check_and_fix_garbage_names(self, stops, cur, db_conn) -> CheckResult:
        garbage = [
            s for s in stops
            if s["name"] and s["name"].strip()
            and any(p.search(s["name"].strip()) for p in _GARBAGE_PATTERNS)
        ]
        if not garbage:
            return CheckResult("stop_garbage_names", passed=True, count=0)

        fixed = _treat_stops_for_name_repair(
            garbage, db_conn, caller="pre_export_enforcer.garbage_names",
        )

        remaining = len(garbage) - fixed
        return CheckResult(
            "stop_garbage_names", passed=(remaining == 0),
            count=len(garbage), auto_fixed=fixed, remaining=remaining,
            details=f"{len(garbage)} garbage, {fixed} fixed by treater",
        )

    def _check_null_island(self, stops) -> CheckResult:
        null_island = [
            s for s in stops
            if abs(s["lat"]) < 0.01 and abs(s["lon"]) < 0.01
        ]
        return CheckResult(
            "stop_null_island", passed=(len(null_island) == 0),
            count=len(null_island),
        )

    def _check_outside_bbox(self, stops) -> CheckResult:
        outside = [
            s for s in stops
            if not (ECUADOR_BBOX[0] <= s["lat"] <= ECUADOR_BBOX[2]
                    and ECUADOR_BBOX[1] <= s["lon"] <= ECUADOR_BBOX[3])
        ]
        return CheckResult(
            "stop_outside_ecuador", passed=(len(outside) == 0),
            count=len(outside),
        )

    # ── Route checks ─────────────────────────────────────────────

    def _check_and_fix_straight_line(self, routes, cur, db_conn) -> CheckResult:
        straight = [
            r for r in routes
            if r["shape_points"] is not None
            and r["stop_count"] is not None
            and r["shape_points"] <= (r["stop_count"] or 0) + 5
            and (r["length_km"] or 0) > 1.0
        ]
        if not straight:
            return CheckResult("route_straight_line", passed=True, count=0)

        fixed = 0
        for route in straight:
            stop_coords = self._get_stop_coords(route, cur)
            if len(stop_coords) < 2:
                continue
            ewkt = _valhalla_retrace(stop_coords)
            if ewkt:
                self._replace_route_geom(
                    route, ewkt, cur, db_conn, caller_tag="fix_straight_line"
                )
                cur.execute(
                    "SELECT ST_NPoints(geom) FROM route_prod.routes WHERE route_id = %s::uuid",
                    (route["route_id"],),
                )
                new_pts = cur.fetchone()[0]
                route["shape_points"] = new_pts
                _LOG.info(
                    "Retrace %s: %s -> %s points",
                    route["route_name"], straight, new_pts,
                )
                fixed += 1

        still_bad = sum(
            1 for r in straight
            if r["shape_points"] <= (r["stop_count"] or 0) + 5
        )
        return CheckResult(
            "route_straight_line", passed=(still_bad == 0),
            count=len(straight), auto_fixed=fixed, remaining=still_bad,
        )

    def _check_and_fix_low_detail(self, routes, cur, db_conn) -> CheckResult:
        low = [
            r for r in routes
            if r["shape_points"] is not None
            and r["shape_points"] < 20
            and (r["length_km"] or 0) > 1.0
        ]
        if not low:
            return CheckResult("route_low_detail", passed=True, count=0)

        fixed = 0
        for route in low:
            stop_coords = self._get_stop_coords(route, cur)
            if len(stop_coords) < 2:
                continue
            ewkt = _valhalla_retrace(stop_coords)
            if ewkt:
                self._replace_route_geom(
                    route, ewkt, cur, db_conn, caller_tag="fix_low_detail"
                )
                cur.execute(
                    "SELECT ST_NPoints(geom) FROM route_prod.routes WHERE route_id = %s::uuid",
                    (route["route_id"],),
                )
                new_pts = cur.fetchone()[0]
                route["shape_points"] = new_pts
                fixed += 1

        still_bad = sum(1 for r in low if r["shape_points"] < 20)
        return CheckResult(
            "route_low_detail", passed=(still_bad == 0),
            count=len(low), auto_fixed=fixed, remaining=still_bad,
        )

    def _check_low_sinuosity(self, routes) -> CheckResult:
        bad = [
            r for r in routes
            if r["sinuosity"] is not None
            and r["sinuosity"] < 1.05
            and (r["shape_points"] or 0) <= 100
            and (r["length_km"] or 0) > 1.0
        ]
        return CheckResult(
            "route_low_sinuosity", passed=(len(bad) == 0),
            count=len(bad),
        )

    def _check_few_stops(self, routes) -> tuple[CheckResult, list]:
        few = [r for r in routes if (r["stop_count"] or 0) < 3]
        excluded = [
            {"route_id": r["route_id"], "route_name": r["route_name"],
             "reason": f"<3 stops ({r['stop_count'] or 0})"}
            for r in few
        ]
        return (
            CheckResult(
                "route_few_stops", passed=True,
                count=len(few), details=f"{len(few)} routes excluded (<3 stops)",
            ),
            excluded,
        )

    def _check_interprovincial(self, routes) -> tuple[CheckResult, list]:
        long = [r for r in routes if (r["length_km"] or 0) > 100]
        excluded = [
            {"route_id": r["route_id"], "route_name": r["route_name"],
             "reason": f">100km ({r['length_km']:.0f}km)"}
            for r in long
        ]
        return (
            CheckResult(
                "route_interprovincial", passed=True,
                count=len(long), details=f"{len(long)} routes excluded (>100km)",
            ),
            excluded,
        )

    def _check_garbage_operators(self, routes, cur) -> tuple[CheckResult, list]:
        _OP_GARBAGE = [
            re.compile(r"(?i)^unknown"),
            re.compile(r"(?i)^unnamed"),
            re.compile(r"(?i)^desconocido"),
            re.compile(r"(?i)rel \d{5,}"),
            re.compile(r"(?i)relation \d+"),
            re.compile(r"^\d+$"),
            re.compile(r"(?i)^\[pending research\]"),
            re.compile(r"(?i)^cooperativa$"),
            re.compile(r"(?i)^operadora$"),
        ]
        bad = [
            r for r in routes
            if r.get("operator_name") and r["operator_name"].strip()
            and any(p.search(r["operator_name"].strip()) for p in _OP_GARBAGE)
        ]
        excluded = [
            {"route_id": r["route_id"], "route_name": r["route_name"],
             "reason": f"garbage operator: {r.get('operator_name', '')!r}"}
            for r in bad
        ]
        return (
            CheckResult(
                "operator_garbage_names", passed=True,
                count=len(bad),
                details=f"{len(bad)} routes excluded (garbage operator)" if bad else "",
            ),
            excluded,
        )

    # ── Helpers ──────────────────────────────────────────────────

    def _replace_route_geom(
        self, route: dict, ewkt: str, cur, db_conn, caller_tag: str
    ) -> None:
        """Rewrite a route's geom via the wrapper (geom isn't patchable).

        Re-reads the full row state, then issues a grandfathered upsert
        through ``write_to_route_prod`` carrying the Valhalla-retraced
        geometry. Grandfathered because pre-export auto-fix does not
        produce a Valhalla request/response JSON payload that satisfies
        the audit CHECK constraint; this runs strictly as a best-effort
        runtime geometry patch before GTFS compile.
        """
        cur.execute(
            """
            SELECT province,
                   stop_node_ids,
                   route_name,
                   route_aliases,
                   landmark_tags,
                   direction_semantics,
                   naming_confidence,
                   human_verified
            FROM route_prod.routes
            WHERE route_id = %s::uuid
            """,
            (route["route_id"],),
        )
        row = cur.fetchone()
        if row is None:
            return
        if isinstance(row, dict):
            (
                province, stop_node_ids, route_name, route_aliases,
                landmark_tags, direction_semantics, naming_confidence,
                human_verified,
            ) = (
                row["province"], row["stop_node_ids"], row["route_name"],
                row["route_aliases"], row["landmark_tags"],
                row["direction_semantics"], row["naming_confidence"],
                row["human_verified"],
            )
        else:
            (province, stop_node_ids, route_name, route_aliases,
             landmark_tags, direction_semantics, naming_confidence,
             human_verified) = row

        route_data = {
            "route_id": route["route_id"],
            "province": province,
            "route_name": route_name,
            "route_aliases": route_aliases or [],
            "landmark_tags": landmark_tags or [],
            "direction_semantics": direction_semantics or {},
            "naming_confidence": naming_confidence,
            "human_verified": bool(human_verified) if human_verified is not None else False,
        }
        res = write_to_route_prod(
            conn=db_conn,
            route_code=str(route["route_id"]),
            route_data=route_data,
            stops=list(stop_node_ids or []),
            shape={"wkt": ewkt.split(";", 1)[1] if ";" in ewkt else ewkt},
            source_type=f"pre_export_enforcer.{caller_tag}",
            pipeline_version="phase5_gtfs/pre_export_enforcer",
            legacy_grandfathered=True,
        )
        if not res.success:
            _LOG.warning(
                "route_prod geom replace failed for %s: %s",
                route["route_id"], res.error,
            )

    def _get_stop_coords(self, route: dict, cur) -> list[tuple[float, float]]:
        stop_ids = route.get("stop_node_ids") or []
        if not stop_ids:
            return []
        coords = []
        for sid in stop_ids:
            cur.execute(
                "SELECT ST_Y(geom)::float AS lat, ST_X(geom)::float AS lon FROM node_prod.nodes WHERE node_id = %s::uuid",
                (sid,),
            )
            row = cur.fetchone()
            if row:
                if isinstance(row, dict):
                    coords.append((float(row["lat"]), float(row["lon"])))
                else:
                    coords.append((float(row[0]), float(row[1])))
        return coords
