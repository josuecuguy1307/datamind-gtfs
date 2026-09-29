from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from datamind_console.db import db_conn, fetch_one, fetch_all
from datamind_console.ui.components.maps import MapPoint

Json = Dict[str, Any]


# ============================================================
# Time helpers
# ============================================================

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _table(name: str) -> str:
    return name


# ============================================================
# Safe query helpers (never crash dashboard)
# ============================================================

def _safe_one(sql: str, params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        with db_conn(readonly=True) as conn:
            return fetch_one(conn, sql, params) or {}
    except Exception:
        return {}


def _safe_all(sql: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        with db_conn(readonly=True) as conn:
            return fetch_all(conn, sql, params) or []
    except Exception:
        return []


def _safe_scalar(sql: str, params: Dict[str, Any], key: str = "n", default: int = 0) -> int:
    row = _safe_one(sql, params)
    try:
        return int(row.get(key, default) or default)
    except Exception:
        return default


# ============================================================
# Typed row objects (optional but nice)
# ============================================================

@dataclass(frozen=True)
class PhaseSummaryRow:
    phase: int
    total: int
    approve: int
    reject: int
    edit: int
    publish: int
    approve_rate: float


# ============================================================
# SINGLE SERVICE (everything)
# ============================================================

class AnalyticsService:
    """
    One service to rule them all:
    - console decisions analytics
    - cross-phase inventory counts
    - health checks
    - visualization-ready tables (time series, histograms, maps)
    """

    def __init__(
        self,
        *,
        # Console tables
        t_phase_decisions: str = "console.phase_decisions",
        t_audit_events: str = "console.audit_events",

        # Phase PROD tables (inventory truth)
        t_node_prod_nodes: str = "node_prod.nodes",
        t_geo_prod_places: str = "geo_prod.places",
        t_geo_prod_node_place_map: str = "geo_prod.node_place_map",
        t_route_prod_routes: str = "route_prod.routes",
    ):
        self.T_DECISIONS = _table(t_phase_decisions)
        self.T_AUDIT = _table(t_audit_events)

        self.T_NODE_PROD = _table(t_node_prod_nodes)
        self.T_GEO_PLACES = _table(t_geo_prod_places)
        self.T_GEO_NODE_PLACE = _table(t_geo_prod_node_place_map)
        self.T_ROUTE_PROD = _table(t_route_prod_routes)

    # ============================================================
    # 0) DASHBOARD INVENTORY (fixes your missing count_* funcs)
    # ============================================================

    def count_nodes(self, *, node_type: Optional[str] = None) -> int:
        where = ""
        params: Dict[str, Any] = {}
        if node_type:
            where = "WHERE node_type = %(t)s"
            params["t"] = node_type.upper().strip()

        sql = f"SELECT COUNT(*) AS n FROM {self.T_NODE_PROD} {where}"
        return _safe_scalar(sql, params, "n", 0)
    

    def count_stops(self) -> int:
        return self.count_nodes(node_type="STOP")

    def count_poi(self) -> int:
        return self.count_nodes(node_type="POI")

    def count_places(self, *, place_type: Optional[str] = None) -> int:
        where = ""
        params: Dict[str, Any] = {}
        if place_type:
            where = "WHERE place_type = %(t)s"
            params["t"] = place_type.upper().strip()

        sql = f"SELECT COUNT(*) AS n FROM {self.T_GEO_PLACES} {where}"
        return _safe_scalar(sql, params, "n", 0)

    def count_node_place_mappings(self) -> int:
        sql = f"SELECT COUNT(*) AS n FROM {self.T_GEO_NODE_PLACE}"
        return _safe_scalar(sql, {}, "n", 0)

    def count_routes(self, *, verified_only: bool = False) -> int:
        where = ""
        params: Dict[str, Any] = {}
        if verified_only:
            where = "WHERE human_verified = true"

        sql = f"SELECT COUNT(*) AS n FROM {self.T_ROUTE_PROD} {where}"
        return _safe_scalar(sql, params, "n", 0)
    
            
    def count_verified_routes(self) -> int:
        return self.count_routes(verified_only=True)

    def count_total_routes(self) -> int:
        return self.count_routes()

    def count_total_nodes(self) -> int:
        return self.count_nodes()

    def count_total_places(self) -> int:
        return self.count_places()

    def count_pending_reviews(self, *, since_days: int = 30) -> int:
        """
        Dashboard: 'Pending Reviews'
        Best-effort + safe:
        - Try status='PENDING' if column exists.
        - Else try decision IS NULL (if exists).
        - Else treat decision='EDIT' as pending review proxy.
        - Never crash; returns 0 if query can't run.
        """
        since = _utc_now() - timedelta(days=int(since_days))

        # 1) If a 'status' column exists and uses 'PENDING'
        sql_status = f"""
        SELECT COUNT(*) AS n
        FROM {self.T_DECISIONS}
        WHERE created_at >= %(since)s
        AND status = 'PENDING'
        """
        n = _safe_scalar(sql_status, {"since": since}, "n", 0)
        if n > 0:
            return n

        # 2) If decision can be NULL (some schemas do that)
        sql_null = f"""
        SELECT COUNT(*) AS n
        FROM {self.T_DECISIONS}
        WHERE created_at >= %(since)s
        AND decision IS NULL
        """
        n2 = _safe_scalar(sql_null, {"since": since}, "n", 0)
        if n2 > 0:
            return n2

        # 3) Proxy: treat EDIT as "needs review"
        sql_edit = f"""
        SELECT COUNT(*) AS n
        FROM {self.T_DECISIONS}
        WHERE created_at >= %(since)s
        AND decision = 'EDIT'
        """
        return _safe_scalar(sql_edit, {"since": since}, "n", 0)

    _model_table_cache: Optional[str] = None

    def count_models(self) -> int:
        """
        Dashboard: 'Models'
        We don't know your exact model registry table yet, so we:
        - check a few common table names safely (Postgres)
        - return count from the first one that exists
        - otherwise return 0 (never crash dashboard)
        Caches discovered table name to avoid repeated lookups.
        """
        if AnalyticsService._model_table_cache is not None:
            return _safe_scalar(
                f"SELECT COUNT(*) AS n FROM {AnalyticsService._model_table_cache}", {}, "n", 0
            )

        candidates = [
            "console.models",
            "console.model_registry",
            "console.ml_models",
            "ml_prod.models",
            "model_prod.models",
        ]

        for tbl in candidates:
            row = _safe_one("SELECT to_regclass(%(t)s) AS reg", {"t": tbl})
            if row.get("reg"):
                AnalyticsService._model_table_cache = tbl
                return _safe_scalar(f"SELECT COUNT(*) AS n FROM {tbl}", {}, "n", 0)

        return 0


    def latest_updates(self) -> Json:
        # Works even if some tables don’t exist (safe helpers)
        sql_nodes = f"SELECT MAX(updated_at) AS ts FROM {self.T_NODE_PROD}"
        sql_places = f"SELECT MAX(updated_at) AS ts FROM {self.T_GEO_PLACES}"
        sql_routes = f"SELECT MAX(updated_at) AS ts FROM {self.T_ROUTE_PROD}"

        n = _safe_one(sql_nodes, {})
        g = _safe_one(sql_places, {})
        r = _safe_one(sql_routes, {})

        return {
            "nodes_updated_at": n.get("ts"),
            "places_updated_at": g.get("ts"),
            "routes_updated_at": r.get("ts"),
        }

    def dashboard_kpis(self) -> Json:
        """
        One-call KPI bundle for Dashboard tiles.
        """
        return {
            "nodes_total": self.count_nodes(),
            "nodes_stops": self.count_nodes(node_type="STOP"),
            "nodes_poi": self.count_nodes(node_type="POI"),
            "places_total": self.count_places(),
            "routes_total": self.count_routes(),
            "routes_verified": self.count_routes(verified_only=True),
            **self.latest_updates(),
        }

    # ============================================================
    # 1) CONSOLE DECISIONS ANALYTICS (your existing stuff)
    # ============================================================

    def recent_decisions(
        self,
        *,
        limit: int = 100,
        phase: Optional[int] = None,
        user_id: Optional[str] = None,
        since_days: Optional[int] = None,
    ) -> List[Json]:
        where: List[str] = []
        params: Dict[str, Any] = {"lim": int(limit)}

        if phase is not None:
            where.append("phase = %(phase)s")
            params["phase"] = int(phase)

        if user_id is not None:
            where.append("user_id = %(user_id)s::uuid")
            params["user_id"] = str(user_id)

        if since_days is not None:
            since = _utc_now() - timedelta(days=int(since_days))
            where.append("created_at >= %(since)s")
            params["since"] = since

        where_sql = "WHERE " + " AND ".join(where) if where else ""

        sql = f"""
        SELECT
          decision_id,
          phase,
          item_id,
          candidate_id,
          score_at_decision,
          decision,
          reason_code,
          notes,
          user_id,
          created_at
        FROM {self.T_DECISIONS}
        {where_sql}
        ORDER BY created_at DESC
        LIMIT %(lim)s
        """
        return _safe_all(sql, params)

    def phase_summary(self, *, since_days: int = 30) -> List[PhaseSummaryRow]:
        since = _utc_now() - timedelta(days=int(since_days))

        sql = f"""
        SELECT
          phase,
          COUNT(*) AS total,
          SUM(CASE WHEN decision='APPROVE' THEN 1 ELSE 0 END) AS approve,
          SUM(CASE WHEN decision='REJECT' THEN 1 ELSE 0 END) AS reject,
          SUM(CASE WHEN decision='EDIT' THEN 1 ELSE 0 END) AS edit,
          SUM(CASE WHEN decision='PUBLISH' THEN 1 ELSE 0 END) AS publish
        FROM {self.T_DECISIONS}
        WHERE created_at >= %(since)s
        GROUP BY phase
        ORDER BY phase ASC
        """
        rows = _safe_all(sql, {"since": since})

        out: List[PhaseSummaryRow] = []
        for r in rows:
            total = int(r.get("total") or 0)
            approve = int(r.get("approve") or 0)
            reject = int(r.get("reject") or 0)
            edit = int(r.get("edit") or 0)
            publish = int(r.get("publish") or 0)

            denom = approve + reject
            approve_rate = float(approve / denom) if denom else 0.0

            out.append(
                PhaseSummaryRow(
                    phase=int(r.get("phase") or 0),
                    total=total,
                    approve=approve,
                    reject=reject,
                    edit=edit,
                    publish=publish,
                    approve_rate=approve_rate,
                )
            )
        return out

    def decision_timeseries(
        self,
        *,
        phase: int,
        days: int = 60,
        tz: str = "UTC",
    ) -> List[Json]:
        since = _utc_now() - timedelta(days=int(days))

        sql = f"""
        SELECT
          DATE_TRUNC('day', created_at AT TIME ZONE %(tz)s) AS day,
          SUM(CASE WHEN decision='APPROVE' THEN 1 ELSE 0 END) AS approve,
          SUM(CASE WHEN decision='REJECT' THEN 1 ELSE 0 END) AS reject,
          SUM(CASE WHEN decision='EDIT' THEN 1 ELSE 0 END) AS edit,
          SUM(CASE WHEN decision='PUBLISH' THEN 1 ELSE 0 END) AS publish,
          COUNT(*) AS total
        FROM {self.T_DECISIONS}
        WHERE phase = %(phase)s
          AND created_at >= %(since)s
        GROUP BY 1
        ORDER BY 1 ASC
        """
        return _safe_all(sql, {"phase": int(phase), "since": since, "tz": tz})

    def reject_reasons(
        self,
        *,
        phase: int,
        since_days: int = 90,
        limit: int = 25,
    ) -> List[Json]:
        since = _utc_now() - timedelta(days=int(since_days))

        sql = f"""
        SELECT
          COALESCE(reason_code, 'UNKNOWN') AS reason_code,
          COUNT(*) AS n
        FROM {self.T_DECISIONS}
        WHERE phase = %(phase)s
          AND decision = 'REJECT'
          AND created_at >= %(since)s
        GROUP BY 1
        ORDER BY n DESC
        LIMIT %(lim)s
        """
        return _safe_all(sql, {"phase": int(phase), "since": since, "lim": int(limit)})

    def score_histogram(
        self,
        *,
        phase: int,
        decision: Optional[str] = None,
        since_days: int = 90,
        bins: int = 20,
        score_min: float = 0.0,
        score_max: float = 1.0,
    ) -> List[Json]:
        since = _utc_now() - timedelta(days=int(since_days))

        where = ["phase = %(phase)s", "created_at >= %(since)s", "score_at_decision IS NOT NULL"]
        params: Dict[str, Any] = {
            "phase": int(phase),
            "since": since,
            "bins": int(bins),
            "smin": float(score_min),
            "smax": float(score_max),
        }

        if decision:
            where.append("decision = %(decision)s")
            params["decision"] = decision.upper().strip()

        where_sql = " AND ".join(where)

        sql = f"""
        SELECT
          width_bucket(score_at_decision, %(smin)s, %(smax)s, %(bins)s) AS bucket,
          COUNT(*) AS n,
          MIN(score_at_decision) AS bucket_min,
          MAX(score_at_decision) AS bucket_max
        FROM {self.T_DECISIONS}
        WHERE {where_sql}
        GROUP BY 1
        ORDER BY bucket ASC
        """
        return _safe_all(sql, params)

    def user_activity(
        self,
        *,
        since_days: int = 30,
        limit: int = 50,
    ) -> List[Json]:
        since = _utc_now() - timedelta(days=int(since_days))

        sql = f"""
        SELECT
          user_id,
          COUNT(*) AS actions,
          SUM(CASE WHEN decision='APPROVE' THEN 1 ELSE 0 END) AS approve,
          SUM(CASE WHEN decision='REJECT' THEN 1 ELSE 0 END) AS reject,
          MAX(created_at) AS last_action_at
        FROM {self.T_DECISIONS}
        WHERE created_at >= %(since)s
        GROUP BY user_id
        ORDER BY actions DESC
        LIMIT %(lim)s
        """
        return _safe_all(sql, {"since": since, "lim": int(limit)})

    def training_readiness(self, *, phase: int, since_days: int = 180) -> Json:
        since = _utc_now() - timedelta(days=int(since_days))

        sql = f"""
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN decision='APPROVE' THEN 1 ELSE 0 END) AS approve,
          SUM(CASE WHEN decision='REJECT' THEN 1 ELSE 0 END) AS reject,
          SUM(CASE WHEN score_at_decision IS NOT NULL THEN 1 ELSE 0 END) AS with_score
        FROM {self.T_DECISIONS}
        WHERE phase = %(phase)s
          AND created_at >= %(since)s
        """
        row = _safe_one(sql, {"phase": int(phase), "since": since})

        total = int(row.get("total") or 0)
        approve = int(row.get("approve") or 0)
        reject = int(row.get("reject") or 0)
        with_score = int(row.get("with_score") or 0)

        denom = approve + reject
        approve_rate = float(approve / denom) if denom else 0.0
        score_coverage = float(with_score / total) if total else 0.0

        return {
            "phase": int(phase),
            "total_decisions": total,
            "approve": approve,
            "reject": reject,
            "approve_rate": approve_rate,
            "score_coverage": score_coverage,
        }

    # ============================================================
    # 2) PHASE HEALTH SNAPSHOT (raw/work/prod sanity checks)
    # ============================================================

    def health_snapshot(self) -> Json:
        """
        Minimal health stats that never crash.
        Extend whenever you want.
        """
        out: Json = {}

        # Node prod
        out["node_prod_total"] = self.count_nodes()
        out["node_prod_null_geom"] = _safe_scalar(
            f"SELECT COUNT(*) AS n FROM {self.T_NODE_PROD} WHERE geom IS NULL",
            {},
            "n",
            0,
        )

        # Geo prod
        out["geo_prod_places_total"] = self.count_places()
        out["geo_prod_node_place_map_total"] = _safe_scalar(
            f"SELECT COUNT(*) AS n FROM {self.T_GEO_NODE_PLACE}",
            {},
            "n",
            0,
        )

        # Route prod
        out["route_prod_total"] = self.count_routes()
        out["route_prod_null_geom"] = _safe_scalar(
            f"SELECT COUNT(*) AS n FROM {self.T_ROUTE_PROD} WHERE geom IS NULL",
            {},
            "n",
            0,
        )
        out["route_prod_empty_stop_node_ids"] = _safe_scalar(
            f"SELECT COUNT(*) AS n FROM {self.T_ROUTE_PROD} WHERE COALESCE(array_length(stop_node_ids, 1), 0) = 0",
            {},
            "n",
            0,
        )

        return out

    # ============================================================
    # 3) VISUALIZATION TABLES (charts + maps) — still ONE service
    # ============================================================

    # -----------------------------
    # A) Map points for nodes (STOP/POI)
    # -----------------------------
    def map_nodes(
        self,
        *,
        node_type: Optional[str] = None,
        limit: int = 5000,
    ) -> List[Json]:
        where = []
        params: Dict[str, Any] = {"lim": int(limit)}

        if node_type:
            where.append("node_type = %(t)s")
            params["t"] = node_type.upper().strip()

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        sql = f"""
        SELECT
          node_id,
          node_type,
          name,
          ref,
          operator,
          tag_kind,
          confidence,
          ST_Y(geom) AS lat,
          ST_X(geom) AS lon
        FROM {self.T_NODE_PROD}
        {where_sql}
        ORDER BY confidence DESC NULLS LAST
        LIMIT %(lim)s
        """
        return _safe_all(sql, params)

    def map_places(
        self,
        *,
        place_type: Optional[str] = None,
        limit: int = 5000,
    ) -> List[Json]:
        where = []
        params: Dict[str, Any] = {"lim": int(limit)}

        if place_type:
            where.append("place_type = %(t)s")
            params["t"] = place_type.upper().strip()

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        sql = f"""
        SELECT
          place_id,
          canonical_name,
          place_type,
          node_id,
          confidence,
          mapping_source,
          node_type,
          lat,
          lon
        FROM geo_prod.v_place_points
        {where_sql}
        ORDER BY confidence DESC NULLS LAST, canonical_name ASC
        LIMIT %(lim)s
        """
        return _safe_all(sql, params)

    # -----------------------------
    # B) Route lines for map (as GeoJSON-ish)
    # -----------------------------
    def map_routes(
        self,
        *,
        verified_only: bool = False,
        limit: int = 500,
    ) -> List[Json]:
        where = []
        params: Dict[str, Any] = {"lim": int(limit)}

        if verified_only:
            where.append("human_verified = true")

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        # Return GeoJSON so Streamlit map layers can render it
        sql = f"""
        SELECT
          route_id,
          route_name,
          naming_confidence,
          human_verified,
          ST_AsGeoJSON(geom)::jsonb AS geom_geojson,
          COALESCE(array_length(stop_node_ids, 1), 0) AS stop_count
        FROM {self.T_ROUTE_PROD}
        {where_sql}
        ORDER BY updated_at DESC
        LIMIT %(lim)s
        """
        return _safe_all(sql, params)

    # -----------------------------
    # C) Route length leaderboard (bar chart)
    # -----------------------------
   


    def get_stop_points(
        self,
        *,
        limit: int = 2500,
    ) -> List[MapPoint]:
        """
        Adapter for UI maps.
        Converts node_prod STOP rows → MapPoint objects.
        """
        rows = self.map_nodes(node_type="STOP", limit=limit)

        points: List[MapPoint] = []
        for r in rows:
            lat = r.get("lat")
            lon = r.get("lon")
            if lat is None or lon is None:
                continue

            label = r.get("name") or r.get("ref") or r.get("node_id")

            points.append(
                MapPoint(
                    lat=float(lat),
                    lon=float(lon),
                    label=str(label),
                    value=float(r.get("confidence") or 0.0),
                )
            )

        return points

    def get_phase2_stop_points(
        self,
        *,
        limit: int = 2500,
    ) -> List[MapPoint]:
        """
        Adapter for UI maps.
        Converts geo_prod STOP place points -> MapPoint objects.
        """
        rows = self.map_places(place_type="STOP", limit=limit)

        points: List[MapPoint] = []
        for r in rows:
            lat = r.get("lat")
            lon = r.get("lon")
            if lat is None or lon is None:
                continue

            label = r.get("canonical_name") or r.get("node_id") or r.get("place_id")

            points.append(
                MapPoint(
                    lat=float(lat),
                    lon=float(lon),
                    label=str(label),
                    value=float(r.get("confidence") or 0.0),
                )
            )

        return points

    def route_length_top(
        self,
        *,
        limit: int = 50,
    ) -> List[Json]:
        sql = f"""
        SELECT
          route_id,
          COALESCE(route_name, route_id::text) AS label,
          (ST_Length(geom::geography) / 1000.0) AS km,
          COALESCE(array_length(stop_node_ids, 1), 0) AS stop_count,
          naming_confidence,
          human_verified
        FROM {self.T_ROUTE_PROD}
        WHERE geom IS NOT NULL
        ORDER BY km DESC NULLS LAST
        LIMIT %(lim)s
        """
        return _safe_all(sql, {"lim": int(limit)})

    # -----------------------------
    # D) Stops per route (bar chart)
    # -----------------------------
    def route_stopcount_top(self, *, limit: int = 50) -> List[Json]:
        sql = f"""
        SELECT
          route_id,
          COALESCE(route_name, route_id::text) AS label,
          COALESCE(array_length(stop_node_ids, 1), 0) AS stop_count,
          naming_confidence,
          human_verified
        FROM {self.T_ROUTE_PROD}
        ORDER BY stop_count DESC NULLS LAST
        LIMIT %(lim)s
        """
        return _safe_all(sql, {"lim": int(limit)})

    # -----------------------------
    # E) Naming quality distribution (histogram-ish buckets)
    # -----------------------------
    def naming_confidence_histogram(
        self,
        *,
        bins: int = 20,
        min_c: float = 0.0,
        max_c: float = 1.0,
    ) -> List[Json]:
        sql = f"""
        SELECT
          width_bucket(naming_confidence, %(min)s, %(max)s, %(bins)s) AS bucket,
          COUNT(*) AS n,
          MIN(naming_confidence) AS bucket_min,
          MAX(naming_confidence) AS bucket_max
        FROM {self.T_ROUTE_PROD}
        WHERE naming_confidence IS NOT NULL
        GROUP BY 1
        ORDER BY bucket ASC
        """
        return _safe_all(sql, {"bins": int(bins), "min": float(min_c), "max": float(max_c)})

    # -----------------------------
    # F) Verified vs not verified (pie)
    # -----------------------------
    def routes_verified_breakdown(self) -> Json:
        sql = f"""
        SELECT
          SUM(CASE WHEN human_verified THEN 1 ELSE 0 END) AS verified,
          SUM(CASE WHEN NOT human_verified THEN 1 ELSE 0 END) AS unverified,
          COUNT(*) AS total
        FROM {self.T_ROUTE_PROD}
        """
        row = _safe_one(sql, {})
        return {
            "verified": int(row.get("verified") or 0),
            "unverified": int(row.get("unverified") or 0),
            "total": int(row.get("total") or 0),
        }

    # -----------------------------
    # G) All route geometries (for overview map)
    # -----------------------------
    def get_all_route_geometries(
        self,
        *,
        source_filter: Optional[str] = None,
    ) -> List[Json]:
        """Return route paths as [[lat,lon],...] for the overview map."""
        where = ""
        params: Dict[str, Any] = {}
        if source_filter:
            where = "AND LOWER(r.source) LIKE %(src)s"
            params["src"] = f"%{source_filter.lower()}%"

        sql = f"""
        SELECT
          r.route_id::text AS route_id,
          COALESCE(r.route_name, r.route_id::text) AS route_name,
          r.source,
          COALESCE(array_length(r.stop_node_ids, 1), 0) AS stop_count,
          (ST_Length(r.geom::geography) / 1000.0)::numeric(8,1) AS km,
          ST_AsGeoJSON(r.geom)::json -> 'coordinates' AS coords,
          ST_GeometryType(r.geom) AS gtype
        FROM {self.T_ROUTE_PROD} r
        WHERE r.geom IS NOT NULL {where}
        ORDER BY r.source, r.route_name
        """
        return _safe_all(sql, params)

    def get_route_stops_for_map(
        self,
        *,
        source_filter: Optional[str] = None,
        route_name: Optional[str] = None,
    ) -> List[Json]:
        """Return stop points for routes matching the filter."""
        where_parts = []
        params: Dict[str, Any] = {}
        if source_filter:
            where_parts.append("LOWER(r.source) LIKE %(src)s")
            params["src"] = f"%{source_filter.lower()}%"
        if route_name:
            where_parts.append("r.route_name = %(rname)s")
            params["rname"] = route_name

        where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

        sql = f"""
        SELECT DISTINCT
          n.node_id::text AS stop_id,
          COALESCE(NULLIF(n.name, ''), 'Stop ' || LEFT(n.node_id::text, 8)) AS stop_name,
          ST_Y(n.geom) AS lat,
          ST_X(n.geom) AS lon,
          r.route_name
        FROM {self.T_ROUTE_PROD} r
        JOIN LATERAL unnest(r.stop_node_ids) AS sid(node_id) ON true
        JOIN node_prod.nodes n ON n.node_id = sid.node_id
        {where}
        """
        return _safe_all(sql, params)

    # ============================================================
    # 4) "INSIGHTS"-STYLE OUTPUTS (still same service)
    #    Return dicts shaped for UI: kpis + tables + map_points
    # ============================================================

    def peak_saturation_bundle(self, *, context_key: Optional[str] = None) -> Json:
        """
        You don’t have demand tables yet, so this returns:
        - KPIs from real DB
        - placeholder demand_by_hour (zeros)
        - real map_points from STOP nodes
        """
        kpis = self.dashboard_kpis()

        demand_by_hour = [{"hour": h, "demand": 0} for h in range(24)]
        overcrowding_heatmap: List[Json] = []
        corridor_overload: List[Json] = []

        map_points = self.map_nodes(node_type="STOP", limit=2000)

        return {
            "kpis": {**kpis, "context_key": context_key},
            "demand_by_hour": demand_by_hour,
            "overcrowding_heatmap": overcrowding_heatmap,
            "corridor_overload": corridor_overload,
            "map_points": map_points,
        }


    # ============================================================
    # 5) INSIGHTS API (UI expects these exact method names)
    #    This connects directly to render_insights_view()
    # ============================================================

    def peak_saturation(self, params: Dict[str, Any]) -> Json:
        """
        UI expects:
          {
            kpis: dict,
            demand_by_hour: df-like,
            overcrowding_heatmap: df-like,
            corridor_overload: df-like,
            map_points: df-like
          }
        """
        context_key = params.get("context_key")

        kpis = self.dashboard_kpis()
        kpis["context_key"] = context_key

        # Placeholder until you have passenger count / tap-in / GTFS-RT load
        demand_by_hour = [{"hour": h, "demand": 0} for h in range(24)]

        # Placeholder heatmap (you can pivot later)
        overcrowding_heatmap: List[Json] = []

        # Corridor overload proxy: use stop_count + route length
        # (this is not "true overload", but it gives a meaningful table now)
        length_top = self.route_length_top(limit=50)
        overload_rows: List[Json] = []
        for r in length_top:
            overload_rows.append(
                {
                    "route_id": r.get("route_id"),
                    "label": r.get("label"),
                    "km": float(r.get("km") or 0.0),
                    "stop_count": int(r.get("stop_count") or 0),
                    # proxy score: more stops per km -> "denser corridor"
                    "density_stops_per_km": (
                        (int(r.get("stop_count") or 0) / float(r.get("km") or 1.0))
                        if float(r.get("km") or 0.0) > 0
                        else 0.0
                    ),
                    "naming_confidence": r.get("naming_confidence"),
                    "human_verified": r.get("human_verified"),
                }
            )

        # Map points: plot stops (real)
        map_points = self.map_nodes(node_type="STOP", limit=2500)

        return {
            "kpis": kpis,
            "demand_by_hour": demand_by_hour,
            "overcrowding_heatmap": overcrowding_heatmap,
            "corridor_overload": overload_rows,
            "map_points": map_points,
        }

    def efficiency_indicators(self, params: Dict[str, Any]) -> Json:
        """
        UI expects:
          { kpis, km_per_route, vehicles_needed, cost_model, map_points }
        """
        fare = float(params.get("fare_usd") or 0.35)
        cost_per_km = float(params.get("cost_per_km") or 0.65)
        cap = int(params.get("capacity_per_vehicle") or 70)

        # Use real route lengths
        km_rows = self.route_length_top(limit=200)
        # Convert into a "km_per_route" table the UI can chart
        km_per_route = [
            {"route_id": r["route_id"], "route": r.get("label"), "km": float(r.get("km") or 0.0)}
            for r in km_rows
        ]

        # Simple ops proxy:
        # Suppose each vehicle can cover X km/day. You can tune this later.
        km_per_vehicle_day = 180.0
        vehicles_needed: List[Json] = []
        total_km = 0.0

        for r in km_rows:
            km = float(r.get("km") or 0.0)
            total_km += km
            v_need = int((km / km_per_vehicle_day) + 0.999) if km > 0 else 0
            vehicles_needed.append(
                {
                    "route_id": r["route_id"],
                    "route": r.get("label"),
                    "km": km,
                    "vehicles_est": v_need,
                    "assumption_km_per_vehicle_day": km_per_vehicle_day,
                }
            )

        # Cost model (scenario layer)
        # You don’t have demand yet, so this is mostly structure.
        cost_model = {
            "fare_usd": fare,
            "cost_per_km_usd": cost_per_km,
            "capacity_per_vehicle": cap,
            "total_route_km_modeled": total_km,
            "total_cost_est_usd": total_km * cost_per_km,
            "note": "Demand/revenue not computed yet (no passenger counts). This is cost-only baseline.",
        }

        # KPIs
        kpis = self.dashboard_kpis()
        kpis.update(
            {
                "fare_usd": fare,
                "cost_per_km_usd": cost_per_km,
                "capacity_per_vehicle": cap,
                "modeled_routes": len(km_rows),
                "modeled_total_km": round(total_km, 2),
            }
        )

        # Map points: show routes’ stop points (real)
        map_points = self.map_nodes(node_type="STOP", limit=2500)

        return {
            "kpis": kpis,
            "km_per_route": km_per_route,
            "vehicles_needed": vehicles_needed,
            "cost_model": cost_model,
            "map_points": map_points,
        }

    def reliability(self, params: Dict[str, Any]) -> Json:
        """
        UI expects:
          { kpis, travel_time_volatility, headway_proxy, delay_concentration, map_points }
        Without real AVL/GTFS-RT history, we return placeholders.
        """
        # Placeholder volatility series
        travel_time_volatility = [{"time": i, "volatility": 0.0} for i in range(24)]

        # Headway proxy: use stop spacing rough proxy from stop_count/km
        # (Again: not real headways, but a stable "reliability proxy" table.)
        rows = self.route_length_top(limit=80)
        headway_proxy: List[Json] = []
        for r in rows:
            km = float(r.get("km") or 0.0)
            stops = int(r.get("stop_count") or 0)
            spacing = (km * 1000.0 / max(stops, 1)) if km > 0 else 0.0
            headway_proxy.append(
                {
                    "route_id": r["route_id"],
                    "route": r.get("label"),
                    "km": km,
                    "stop_count": stops,
                    "avg_stop_spacing_m_proxy": round(spacing, 1),
                }
            )

        delay_concentration: List[Json] = []  # needs realtime logs later
        map_points = self.map_nodes(node_type="STOP", limit=2500)

        kpis = self.dashboard_kpis()
        kpis["note"] = "Reliability is proxy-only until GTFS-RT / telemetry history exists."

        return {
            "kpis": kpis,
            "travel_time_volatility": travel_time_volatility,
            "headway_proxy": headway_proxy,
            "delay_concentration": delay_concentration,
            "map_points": map_points,
        }

    def economics_ops(self, params: Dict[str, Any]) -> Json:
        """
        UI expects:
          { kpis, profitability_by_route, subsidy_estimate, scenario_table }
        With no passenger counts, we return cost-focused scenario output.
        """
        fare = float(params.get("fare_usd") or 0.35)
        cost_per_km = float(params.get("cost_per_km") or 0.65)
        cap = int(params.get("capacity_per_vehicle") or 70)

        routes = self.route_length_top(limit=200)

        # Profitability by route (placeholder revenue = 0, cost = km*cost_per_km)
        profitability_by_route: List[Json] = []
        for r in routes:
            km = float(r.get("km") or 0.0)
            cost = km * cost_per_km
            revenue = 0.0  # until you have pax counts / tickets
            profitability_by_route.append(
                {
                    "route_id": r["route_id"],
                    "route": r.get("label"),
                    "km": km,
                    "cost_est_usd": round(cost, 2),
                    "revenue_est_usd": round(revenue, 2),
                    "profit_est_usd": round(revenue - cost, 2),
                    "note": "Revenue=0 placeholder (needs passengers).",
                }
            )

        total_cost = sum(x["cost_est_usd"] for x in profitability_by_route)

        subsidy_estimate = [
            {
                "scenario": "cost_coverage",
                "total_cost_est_usd": round(total_cost, 2),
                "required_subsidy_if_revenue_zero_usd": round(total_cost, 2),
                "note": "This becomes meaningful after you add demand/revenue.",
            }
        ]

        # Scenario table: vary fare and cost/km slightly
        scenario_table: List[Json] = []
        for f in [max(0.0, fare - 0.10), fare, fare + 0.10]:
            for c in [max(0.0, cost_per_km - 0.10), cost_per_km, cost_per_km + 0.10]:
                scenario_table.append(
                    {
                        "fare_usd": round(f, 2),
                        "cost_per_km_usd": round(c, 2),
                        "capacity": cap,
                        "total_cost_est_usd": round(sum(float(r.get("km") or 0.0) * c for r in routes), 2),
                        "note": "Revenue not modeled yet.",
                    }
                )

        kpis = self.dashboard_kpis()
        kpis.update(
            {
                "fare_usd": fare,
                "cost_per_km_usd": cost_per_km,
                "capacity_per_vehicle": cap,
                "modeled_routes": len(routes),
                "total_cost_est_usd": round(total_cost, 2),
            }
        )

        return {
            "kpis": kpis,
            "profitability_by_route": profitability_by_route,
            "subsidy_estimate": subsidy_estimate,
            "scenario_table": scenario_table,
        }
 

 
