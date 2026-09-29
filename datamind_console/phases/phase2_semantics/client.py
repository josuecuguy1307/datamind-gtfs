# phase2_client.py
from __future__ import annotations

import os
import sys
import subprocess
import json
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote_plus
import uuid
from time import perf_counter
import unicodedata

import psycopg2
import psycopg2.extras
from psycopg2.extras import RealDictCursor

import re


from phase2_semantics.src.settings import (
    SEARCH_TOP_K,
)

from datamind_console.persistence import (
    delete_routes_prod_bulk,
    prune_stop_node_id_from_routes,
)

_SOURCE_COMPONENT_PHASE2 = "phase2_client"
_PIPELINE_VERSION_PRUNE_BULK = "phase2_client.delete_stops_bulk"
_PIPELINE_VERSION_PRUNE_SINGLE = "phase2_client.delete_stop_single"
_PIPELINE_VERSION_ROUTES_BULK = "phase2_client.clear_route_prod_bulk"
_PIPELINE_VERSION_ROUTES_SINGLE = "phase2_client.clear_route_prod_single"

# Legacy fallback table used by older runs.
DEFAULT_T_ALIAS_VECTORS = "place_alias_vectors"

# -------------------------------------------------------------------
# Optional: reuse your canonical db_conn if it exists
# -------------------------------------------------------------------
try:
    # Phase2 repo style (what you pasted)
    from src.db.conn import db_conn as _db_conn  # type: ignore
except Exception:
    _db_conn = None


psycopg2.extras.register_uuid()


# -------------------------------------------------------------------
# Defaults (match your pasted Phase 2 SQL / scripts)
# -------------------------------------------------------------------
DEFAULT_T_PLACE_SETS = "geo_work.place_candidate_sets"      # place_set_id
DEFAULT_T_PLACE_CANDS = "geo_work.place_candidates"         # place_candidate_id, place_set_id
DEFAULT_T_ALIAS_CANDS = "geo_work.alias_candidates"         # alias_candidate_id, place_candidate_id
DEFAULT_T_METRICS = "geo_work.place_set_metrics"            # place_set_id, metrics, updated_at (if you have it)
DEFAULT_T_SELECTION_LOG = "geo_work.selection_log"          # optional
DEFAULT_T_NAME_CANDS = "geo_work.place_name_candidates"
DEFAULT_T_NAME_FEEDBACK = "geo_work.place_name_feedback"
DEFAULT_T_MODEL_REGISTRY = "geo_work.model_registry"
DEFAULT_T_NODE_GEO_CONTEXT = "geo_work.node_geo_context"
DEFAULT_T_POI_STOP_FEEDBACK = "geo_work.poi_stop_feedback"

DEFAULT_T_PROD_PLACES = "geo_prod.places"
DEFAULT_T_PROD_ALIASES = "geo_prod.place_aliases"
DEFAULT_T_PROD_NODE_MAP = "geo_prod.node_place_map"         # optional (if you created it)


# -------------------------------------------------------------------
# Domain outputs (nice for UI)
# -------------------------------------------------------------------
@dataclass(frozen=True)

class ApproveSummary:
    place_set_id: str
    n_places_written: int
    n_aliases_written: int
    n_nodes_mapped: int
    reason: str

# -------------------------------------------------------------------
# Small DB helpers (self-contained fallback)
# -------------------------------------------------------------------
def _require_db_dsn() -> str:
    local_only = os.getenv("DATAMIND_LOCAL_ONLY_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
    if local_only:
        dsn = os.getenv("LOCAL_DB_DSN") or os.getenv("DATAMIND_LOCAL_DB_DSN") or os.getenv("DB_DSN_LOCAL")
        if not dsn:
            raise RuntimeError("Local data mode requires LOCAL_DB_DSN; server database fallbacks are disabled.")
        return dsn

    dsn = os.getenv("DB_DSN") or os.getenv("DB_DSN_PHASE2") or os.getenv("DATABASE_URL")
    if not dsn and os.getenv("SUPABASE_DB_HOST"):
        host = os.getenv("SUPABASE_DB_HOST")
        port = os.getenv("SUPABASE_DB_PORT", "5432")
        name = os.getenv("SUPABASE_DB_NAME", "postgres")
        user = os.getenv("SUPABASE_DB_USER", "postgres")
        password = os.getenv("SUPABASE_DB_PASSWORD", "")
        dsn = (
            f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{name}"
            "?sslmode=require"
        )
    if not dsn:
        raise RuntimeError(
            "DB_DSN is not set.\n"
            "Set DB_DSN/DB_DSN_PHASE2/DATABASE_URL or SUPABASE_DB_*.\n"
        )
    return dsn


@contextmanager
def _local_db_conn() -> Any:
    """
    Fallback connection manager (only used if src.db.conn.db_conn isn't importable).
    """
    dsn = _require_db_dsn()
    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _conn_ctx():
    return _db_conn() if callable(_db_conn) else _local_db_conn()


def _q_ident(name: str) -> str:
    # Only for schema.table constants above (no user input).
    return name


_P2_UNKNOWN_NAME_NORMS = {
    "sin nombre",
    "unknown",
    "unnamed",
    "no name",
    "s n",
    "na",
    "n a",
}
_P2_LOWER_CONNECTORS = {"de", "del", "la", "las", "el", "los", "y", "a", "al", "en"}
_P2_GENERIC_NAME_TOKENS = {"parada"}


def _p2_title_case_name(raw: str) -> str:
    txt = re.sub(r"\s+", " ", str(raw or "").strip())
    if not txt:
        return "(sin nombre)"
    parts = txt.split(" ")
    out: list[str] = []
    for i, p in enumerate(parts):
        if not p:
            continue
        low = p.lower()
        if i > 0 and low in _P2_LOWER_CONNECTORS:
            out.append(low)
            continue
        if p.isupper() and p.isalpha() and len(p) <= 4:
            out.append(p)
            continue
        out.append(p[:1].upper() + p[1:].lower())
    return " ".join(out).strip() or "(sin nombre)"


def _p2_normalize_display_name(raw: str) -> str:
    txt = re.sub(r"\s+", " ", str(raw or "").strip())
    if not txt:
        return "Parada"
    low = txt.lower()
    if low.startswith("node_") or low.startswith("node-") or low.startswith("node "):
        return "Parada"
    if low.startswith("stop candidate"):
        return "Parada"
    norm = re.sub(r"[^a-z0-9\\s]+", " ", low)
    norm = re.sub(r"\s+", " ", norm).strip()
    if not norm or norm in _P2_UNKNOWN_NAME_NORMS:
        return "Parada"
    compact = norm.replace(" ", "")
    if compact.isdigit():
        return "Parada"
    titled = _p2_title_case_name(txt)
    if titled == "(sin nombre)":
        return "Parada"
    return titled


def _p2_is_bad_node_name(raw: Any) -> bool:
    txt = re.sub(r"\s+", " ", str(raw or "").strip())
    if not txt:
        return True
    low = txt.lower()
    if low.startswith("node_") or low.startswith("node-") or low.startswith("node "):
        return True
    if low.startswith("stop candidate"):
        return True
    norm = re.sub(r"[^a-z0-9\s]+", " ", low)
    norm = re.sub(r"\s+", " ", norm).strip()
    if not norm or norm in _P2_UNKNOWN_NAME_NORMS:
        return True
    compact = norm.replace(" ", "")
    if compact.isdigit():
        return True
    if not any(ch.isalpha() for ch in txt):
        return True
    return False


def _p2_strip_accents(raw: str) -> str:
    txt = unicodedata.normalize("NFKD", str(raw or ""))
    return "".join(ch for ch in txt if not unicodedata.combining(ch))


def _p2_name_signature(raw: Any) -> str:
    txt = _p2_strip_accents(_p2_normalize_display_name(str(raw or ""))).lower().strip()
    txt = re.sub(r"[^a-z0-9\s]+", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


def _p2_name_tokens(sig: str) -> set[str]:
    return {
        t
        for t in str(sig or "").split()
        if t and t not in _P2_LOWER_CONNECTORS and t not in _P2_GENERIC_NAME_TOKENS
    }


def _p2_row_bad_name(row: Dict[str, Any]) -> bool:
    cached = row.get("_p2_bad_name")
    if isinstance(cached, bool):
        return cached
    out = bool(_p2_is_bad_node_name(row.get("name")))
    row["_p2_bad_name"] = out
    return out


def _p2_row_normalized_name(row: Dict[str, Any]) -> str:
    cached = row.get("_p2_normalized_name")
    if isinstance(cached, str):
        return cached
    out = _p2_normalize_display_name(str(row.get("name") or ""))
    row["_p2_normalized_name"] = out
    return out


def _p2_row_name_signature(row: Dict[str, Any]) -> str:
    cached = row.get("_p2_name_signature")
    if isinstance(cached, str):
        return cached
    out = _p2_name_signature(row.get("name"))
    row["_p2_name_signature"] = out
    return out


def _p2_ref_signature(raw: Any) -> str:
    txt = _p2_strip_accents(str(raw or "")).upper().strip()
    txt = re.sub(r"[^A-Z0-9]+", "", txt)
    return txt


def _p2_row_ref_signature(row: Dict[str, Any]) -> str:
    cached = row.get("_p2_ref_signature")
    if isinstance(cached, str):
        return cached
    out = _p2_ref_signature(row.get("ref"))
    row["_p2_ref_signature"] = out
    return out


def _p2_rows_have_conflicting_refs(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    ref_a = _p2_row_ref_signature(a)
    ref_b = _p2_row_ref_signature(b)
    return bool(ref_a and ref_b and ref_a != ref_b)


def _p2_rows_share_ref(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    ref_a = _p2_row_ref_signature(a)
    ref_b = _p2_row_ref_signature(b)
    return bool(ref_a and ref_b and ref_a == ref_b)


def _p2_to_epoch(v: Any) -> float:
    try:
        if hasattr(v, "timestamp"):
            return float(v.timestamp())
    except Exception:
        pass
    s = str(v or "").strip()
    if not s:
        return 0.0
    try:
        # Local import to keep module-level deps minimal.
        from datetime import datetime

        return float(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0.0


def _p2_haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from hades.geometry.canonical import haversine_m as _canonical_haversine_m

    return float(_canonical_haversine_m(float(lat1), float(lon1), float(lat2), float(lon2)))


def _p2_node_keep_score(row: Dict[str, Any]) -> Tuple[int, int, float, str]:
    name_raw = str(row.get("name") or "").strip()
    norm = _p2_row_normalized_name(row)
    bad = _p2_row_bad_name(row)
    has_letters = 1 if any(ch.isalpha() for ch in name_raw) else 0
    has_ref = 1 if str(row.get("ref") or "").strip() else 0
    updated_ts = _p2_to_epoch(row.get("updated_at"))

    score = 0
    if not bad and norm != "(sin nombre)":
        score += 1000
    score += (150 if has_letters else 0)
    score += (80 if has_ref else 0)
    score += min(len(norm), 50)
    if len(norm.split()) >= 2:
        score += 20
    if bad:
        score -= 600

    nid = str(row.get("node_id") or "")
    return (int(score), int(has_ref), float(updated_ts), nid)


def _p2_row_distance_m(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    return _p2_haversine_m(
        float(a.get("lat") or 0.0),
        float(a.get("lon") or 0.0),
        float(b.get("lat") or 0.0),
        float(b.get("lon") or 0.0),
    )


def _p2_row_name_similarity(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    if _p2_rows_have_conflicting_refs(a, b):
        return 0.0
    sig_a = _p2_row_name_signature(a)
    sig_b = _p2_row_name_signature(b)
    if not sig_a or not sig_b:
        return 0.0
    if sig_a == sig_b:
        return 1.0 if not _p2_rows_share_ref(a, b) else 1.05

    seq = SequenceMatcher(None, sig_a, sig_b).ratio()
    tokens_a = _p2_name_tokens(sig_a)
    tokens_b = _p2_name_tokens(sig_b)
    if not tokens_a or not tokens_b:
        return float(seq)

    inter = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    overlap = float(inter / max(min(len(tokens_a), len(tokens_b)), 1))
    jaccard = float(inter / max(union, 1))
    token_exact = 1.0 if tokens_a == tokens_b else 0.0
    containment = 1.0 if (len(sig_a) >= 10 and len(sig_b) >= 10 and (sig_a in sig_b or sig_b in sig_a)) else 0.0
    ref_bonus = 0.05 if _p2_rows_share_ref(a, b) else 0.0
    return float(max(seq, overlap, jaccard, token_exact, containment) + ref_bonus)


def _p2_rows_have_compatible_names(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    if _p2_rows_have_conflicting_refs(a, b):
        return False

    bad_a = _p2_row_bad_name(a)
    bad_b = _p2_row_bad_name(b)
    if bad_a or bad_b:
        return True

    sig_a = _p2_row_name_signature(a)
    sig_b = _p2_row_name_signature(b)
    if not sig_a or not sig_b:
        return False
    if sig_a == sig_b:
        return True

    tokens_a = _p2_name_tokens(sig_a)
    tokens_b = _p2_name_tokens(sig_b)
    seq = SequenceMatcher(None, sig_a, sig_b).ratio()
    if tokens_a and tokens_b and tokens_a == tokens_b:
        return True

    if tokens_a and tokens_b:
        inter = len(tokens_a & tokens_b)
        overlap = float(inter / max(min(len(tokens_a), len(tokens_b)), 1))
        if overlap >= 0.85 and seq >= 0.80:
            return True
        if min(len(tokens_a), len(tokens_b)) >= 2 and overlap >= 0.66 and seq >= 0.92:
            return True

    if len(sig_a) >= 10 and len(sig_b) >= 10 and (sig_a in sig_b or sig_b in sig_a):
        if tokens_a and tokens_b:
            inter = len(tokens_a & tokens_b)
            overlap = float(inter / max(min(len(tokens_a), len(tokens_b)), 1))
            if overlap >= 0.80 and seq >= 0.88:
                return True
        elif seq >= 0.95:
            return True

    if _p2_rows_share_ref(a, b) and seq >= 0.75:
        return True

    return False


def _p2_semantic_duplicate_groups(members: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ordered = sorted(members, key=_p2_node_keep_score, reverse=True)
    groups: List[Dict[str, Any]] = []

    for member in ordered:
        member_bad = _p2_row_bad_name(member)
        best_group_idx: Optional[int] = None
        best_group_score: float = float("-inf")
        has_good_keeper = any(not _p2_row_bad_name(g["keeper"]) for g in groups)

        for idx, group in enumerate(groups):
            keeper = group["keeper"]
            keeper_bad = _p2_row_bad_name(keeper)

            if member_bad:
                if has_good_keeper and keeper_bad:
                    continue
                score = (1000.0 if not keeper_bad else 0.0) - _p2_row_distance_m(member, keeper)
            else:
                if keeper_bad:
                    continue
                if not _p2_rows_have_compatible_names(member, keeper):
                    continue
                score = (_p2_row_name_similarity(member, keeper) * 1000.0) - _p2_row_distance_m(member, keeper)

            if score > best_group_score:
                best_group_score = score
                best_group_idx = idx

        if best_group_idx is None:
            groups.append({"keeper": member, "members": [member]})
        else:
            groups[best_group_idx]["members"].append(member)

    return groups


def _p2_delete_candidate_sort_key(row: Dict[str, Any]) -> Tuple[int, int, int, str]:
    reason = str(row.get("reason") or "")
    duplicate_priority = 0 if "duplicate" in reason else 1
    combined_priority = 0 if reason == "duplicate+bad_name" else 1
    cluster_size = int(row.get("cluster_size") or 1)
    node_id = str(row.get("node_id") or "")
    return (duplicate_priority, combined_priority, -cluster_size, node_id)


def _p2_cluster_nodes_within_radius(rows: List[Dict[str, Any]], radius_m: float) -> List[List[int]]:
    import math

    n = len(rows or [])
    if n <= 1:
        return [[i] for i in range(n)]

    parent = list(range(n))

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: int, b: int) -> None:
        ra = _find(a)
        rb = _find(b)
        if ra != rb:
            parent[rb] = ra

    # Spatial hashing grid to avoid O(n^2) scans for larger datasets.
    cell_deg = max(float(radius_m) / 111111.0, 1e-7)
    grid: Dict[Tuple[int, int], List[int]] = {}

    for i, r in enumerate(rows):
        lat = float(r.get("lat") or 0.0)
        lon = float(r.get("lon") or 0.0)
        gx = int(math.floor(lat / cell_deg))
        gy = int(math.floor(lon / cell_deg))
        grid.setdefault((gx, gy), []).append(i)

    for i, r in enumerate(rows):
        lat_i = float(r.get("lat") or 0.0)
        lon_i = float(r.get("lon") or 0.0)
        gx = int(math.floor(lat_i / cell_deg))
        gy = int(math.floor(lon_i / cell_deg))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((gx + dx, gy + dy), []):
                    if j <= i:
                        continue
                    rj = rows[j]
                    lat_j = float(rj.get("lat") or 0.0)
                    lon_j = float(rj.get("lon") or 0.0)
                    if _p2_haversine_m(lat_i, lon_i, lat_j, lon_j) <= float(radius_m):
                        _union(i, j)

    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(_find(i), []).append(i)
    return list(groups.values())

class Phase2Client:
    def __init__(
        self,
        *,
        t_place_sets: str = DEFAULT_T_PLACE_SETS,
        t_place_candidates: str = DEFAULT_T_PLACE_CANDS,
        t_alias_candidates: str = DEFAULT_T_ALIAS_CANDS,
        t_metrics: str = DEFAULT_T_METRICS,
        t_selection_log: str = DEFAULT_T_SELECTION_LOG,
        t_name_candidates: str = DEFAULT_T_NAME_CANDS,
        t_name_feedback: str = DEFAULT_T_NAME_FEEDBACK,
        t_model_registry: str = DEFAULT_T_MODEL_REGISTRY,
        t_node_geo_context: str = DEFAULT_T_NODE_GEO_CONTEXT,
        t_poi_stop_feedback: str = DEFAULT_T_POI_STOP_FEEDBACK,
        t_prod_places: str = DEFAULT_T_PROD_PLACES,
        t_prod_aliases: str = DEFAULT_T_PROD_ALIASES,
        t_prod_node_map: str = DEFAULT_T_PROD_NODE_MAP,
    ) -> None:
        self.t_place_sets = t_place_sets
        self.t_place_candidates = t_place_candidates
        self.t_alias_candidates = t_alias_candidates
        self.t_metrics = t_metrics
        self.t_selection_log = t_selection_log
        self.t_name_candidates = t_name_candidates
        self.t_name_feedback = t_name_feedback
        self.t_model_registry = t_model_registry
        self.t_node_geo_context = t_node_geo_context
        self.t_poi_stop_feedback = t_poi_stop_feedback
        self.t_prod_places = t_prod_places
        self.t_prod_aliases = t_prod_aliases
        self.t_prod_node_map = t_prod_node_map

    def _phase2_root(self) -> Path:
        return Path(__file__).resolve().parents[3] / "phase2_semantics"

    def _run_script(
        self,
        script_name: str,
        *,
        args: Optional[List[str]] = None,
        env_overrides: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        root = self._phase2_root()
        script = root / "scripts" / script_name
        if not script.exists():
            raise RuntimeError(f"Missing script: {script}")

        env = os.environ.copy()
        env["PYTHONPATH"] = str(root)
        if env_overrides:
            for k, v in env_overrides.items():
                if v is not None:
                    env[str(k)] = str(v)

        result = subprocess.run(
            [sys.executable, str(script), *(args or [])],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
        )
        out = {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "script": script_name,
        }
        if result.returncode != 0:
            raise RuntimeError(
                f"{script_name} failed.\nSTDOUT:\n{out['stdout']}\n\nSTDERR:\n{out['stderr']}"
            )
        return out

    @staticmethod
    def _script_json_rows(stdout: Any) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for raw_line in str(stdout or "").splitlines():
            line = str(raw_line or "").strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if isinstance(payload, dict):
                rows.append(dict(payload))
        return rows

    def _resolve_place_set_id_from_step_output(self, out: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        summary = dict(out.get("summary") or {})
        for candidate in (summary.get("place_set_id"), out.get("place_set_id")):
            txt = str(candidate or "").strip()
            if txt:
                return txt, "executor_summary"
        for row in reversed(self._script_json_rows(out.get("stdout"))):
            txt = str(row.get("place_set_id") or "").strip()
            if txt:
                return txt, "script_stdout"
        return None, None

    @staticmethod
    def _phase2_env(
        *,
        context_key: Optional[str] = None,
        source_node_set_id: Optional[str] = None,
        place_set_id: Optional[str] = None,
    ) -> Optional[Dict[str, str]]:
        env: Dict[str, str] = {}
        if str(context_key or "").strip():
            env["GEO_CONTEXT_KEY"] = str(context_key).strip()
        if str(source_node_set_id or "").strip():
            env["SOURCE_NODE_SET_ID"] = str(source_node_set_id).strip()
        if str(place_set_id or "").strip():
            env["PLACE_SET_ID"] = str(place_set_id).strip()
        return env or None

    def _latest_place_set_id(self, conn, *, context_key: Optional[str] = None) -> Optional[str]:
        if not self._table_exists(conn, self.t_place_sets):
            return None
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT place_set_id::text AS place_set_id
                FROM {_q_ident(self.t_place_sets)}
                WHERE (%s IS NULL OR context_key = %s)
                ORDER BY created_at DESC NULLS LAST
                LIMIT 1
                """,
                (context_key, context_key),
            )
            row = cur.fetchone() or {}
        value = row.get("place_set_id") if isinstance(row, dict) else (row[0] if row else None)
        txt = str(value or "").strip()
        return txt or None

    def get_latest_place_set_id(self, *, context_key: Optional[str] = None) -> Optional[str]:
        with _conn_ctx() as conn:
            return self._latest_place_set_id(conn, context_key=context_key)

    def _latest_feedback_epoch(self, conn) -> float:
        latest = 0.0
        tables = [self.t_name_feedback, self.t_poi_stop_feedback]
        for table in tables:
            if not self._table_exists(conn, table):
                continue
            with conn.cursor() as cur:
                cur.execute(f"SELECT MAX(created_at) AS ts, COUNT(*)::int AS n FROM {_q_ident(table)}")
                row = cur.fetchone() or {}
            latest = max(latest, _p2_to_epoch((row or {}).get("ts")))
        return float(latest)

    def _feedback_row_count(self, conn) -> int:
        total = 0
        for table in (self.t_name_feedback, self.t_poi_stop_feedback):
            if not self._table_exists(conn, table):
                continue
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*)::int AS n FROM {_q_ident(table)}")
                row = cur.fetchone() or {}
            total += int((row or {}).get("n") or 0)
        return int(total)

    def _latest_model_epoch(self, conn, *, model_names: List[str]) -> float:
        if not self._table_exists(conn, self.t_model_registry):
            return 0.0
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT MAX(updated_at) AS ts
                FROM {_q_ident(self.t_model_registry)}
                WHERE model_name = ANY(%s)
                """,
                (list(model_names),),
            )
            row = cur.fetchone() or {}
        return float(_p2_to_epoch((row or {}).get("ts")))

    def _count_models(self, conn, *, model_names: List[str]) -> int:
        if not self._table_exists(conn, self.t_model_registry):
            return 0
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(*)::int AS n
                FROM {_q_ident(self.t_model_registry)}
                WHERE model_name = ANY(%s)
                """,
                (list(model_names),),
            )
            row = cur.fetchone() or {}
        return int((row or {}).get("n") or 0)

    def get_step35_refresh_plan(self) -> Dict[str, Any]:
        model_names = ["phase2_name_ranker_v1", "phase2_place_type_model_v1"]
        with _conn_ctx() as conn:
            model_registry_exists = self._table_exists(conn, self.t_model_registry)
            feedback_count = self._feedback_row_count(conn)
            model_count = self._count_models(conn, model_names=model_names)
            latest_feedback_epoch = self._latest_feedback_epoch(conn)
            latest_model_epoch = self._latest_model_epoch(conn, model_names=model_names)
        should_run = True
        reason = "new_feedback_available"
        if not model_registry_exists:
            reason = "model_registry_missing"
        elif model_count <= 0:
            reason = "model_missing"
        elif feedback_count <= 0:
            should_run = False
            reason = "no_feedback_rows"
        elif latest_model_epoch > 0 and latest_feedback_epoch <= latest_model_epoch:
            should_run = False
            reason = "models_up_to_date"
        return {
            "should_run": bool(should_run),
            "reason": reason,
            "feedback_count": int(feedback_count),
            "model_count": int(model_count),
            "latest_feedback_epoch": float(latest_feedback_epoch),
            "latest_model_epoch": float(latest_model_epoch),
        }

    def get_step40_refresh_plan(self) -> Dict[str, Any]:
        def _embedding_status(
            conn,
            *,
            source_table: str,
            embedding_table: str,
            missing_reason: str,
            empty_reason: str,
        ) -> Dict[str, Any]:
            source_exists = self._table_exists(conn, source_table)
            embedding_exists = self._table_exists(conn, embedding_table)
            source_count = self._count_table(conn, source_table) if source_exists else 0
            embedding_count = self._count_table(conn, embedding_table) if embedding_exists else 0
            latest_source_epoch = 0.0
            latest_embedding_epoch = 0.0

            if source_exists:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT MAX(updated_at) AS ts FROM {_q_ident(source_table)}")
                    row = cur.fetchone() or {}
                latest_source_epoch = float(_p2_to_epoch((row or {}).get("ts")))

            if embedding_exists:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT MAX(updated_at) AS ts FROM {_q_ident(embedding_table)}")
                    row = cur.fetchone() or {}
                latest_embedding_epoch = float(_p2_to_epoch((row or {}).get("ts")))

            should_run = True
            reason = "stale_embeddings"
            if not source_exists:
                reason = missing_reason
            elif source_count <= 0:
                should_run = False
                reason = empty_reason
            elif not embedding_exists:
                reason = "embedding_table_missing"
            elif embedding_count != source_count:
                reason = "embedding_count_mismatch"
            elif latest_source_epoch > latest_embedding_epoch:
                reason = "source_newer_than_embeddings"
            else:
                should_run = False
                reason = "embeddings_up_to_date"

            return {
                "should_run": bool(should_run),
                "reason": reason,
                "source_count": int(source_count),
                "embedding_count": int(embedding_count),
                "latest_source_epoch": float(latest_source_epoch),
                "latest_embedding_epoch": float(latest_embedding_epoch),
            }

        with _conn_ctx() as conn:
            alias_status = _embedding_status(
                conn,
                source_table=self.t_prod_aliases,
                embedding_table="geo_prod.place_alias_embeddings",
                missing_reason="place_aliases_table_missing",
                empty_reason="no_aliases",
            )
            place_status = _embedding_status(
                conn,
                source_table=self.t_prod_places,
                embedding_table="geo_prod.place_embeddings",
                missing_reason="places_table_missing",
                empty_reason="no_places",
            )

        should_run = bool(alias_status.get("should_run") or place_status.get("should_run"))
        if alias_status.get("should_run"):
            reason = f"aliases_{str(alias_status.get('reason') or 'stale_embeddings')}"
        elif place_status.get("should_run"):
            reason = f"places_{str(place_status.get('reason') or 'stale_embeddings')}"
        else:
            reason = "embeddings_up_to_date"

        return {
            "should_run": bool(should_run),
            "reason": reason,
            "alias_count": int(alias_status.get("source_count") or 0),
            "alias_embedding_count": int(alias_status.get("embedding_count") or 0),
            "latest_alias_epoch": float(alias_status.get("latest_source_epoch") or 0.0),
            "latest_alias_embedding_epoch": float(alias_status.get("latest_embedding_epoch") or 0.0),
            "place_count": int(place_status.get("source_count") or 0),
            "place_embedding_count": int(place_status.get("embedding_count") or 0),
            "latest_place_epoch": float(place_status.get("latest_source_epoch") or 0.0),
            "latest_place_embedding_epoch": float(place_status.get("latest_embedding_epoch") or 0.0),
            "alias_reason": alias_status.get("reason"),
            "place_reason": place_status.get("reason"),
        }

    def run_step_10_extract(
        self,
        *,
        context_key: Optional[str] = None,
        source_node_set_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        out = self._run_script(
            "10_extract_evidence.py",
            env_overrides=self._phase2_env(
                context_key=context_key,
                source_node_set_id=source_node_set_id,
            ),
        )
        out["context_key"] = str(context_key or "").strip() or None
        out["source_node_set_id"] = str(source_node_set_id or "").strip() or None
        return out

    def run_step_15_build_geo_context(self, *, context_key: Optional[str] = None) -> Dict[str, Any]:
        out = self._run_script(
            "15_build_geo_context.py",
            env_overrides=self._phase2_env(context_key=context_key),
        )
        out["context_key"] = str(context_key or "").strip() or None
        return out

    def run_step_20_build_candidates(self, *, context_key: Optional[str] = None) -> Dict[str, Any]:
        out = self._run_script(
            "20_build_candidates.py",
            env_overrides=self._phase2_env(context_key=context_key),
        )
        out["context_key"] = str(context_key or "").strip() or None
        place_set_id, place_set_id_source = self._resolve_place_set_id_from_step_output(out)
        if not place_set_id:
            place_set_id = self.get_latest_place_set_id(context_key=context_key)
            place_set_id_source = "context_lookup" if place_set_id else None
        out["summary"] = {
            "context_key": out["context_key"],
            "place_set_id": place_set_id,
            "place_set_id_source": place_set_id_source,
        }
        return out

    def run_step_25_build_name_candidates(
        self,
        *,
        place_set_id: Optional[str] = None,
        context_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Run Step 25 against the SAME DB connection context used by the UI/client.
        This avoids subprocess env/DSN drift between local/server modes.
        """
        try:
            from phase2_semantics.src.pipeline.naming.build_name_candidates import build_for_place_set
        except Exception:
            # Fallback to script path if in-process import is unavailable.
            psid = str(place_set_id or "").strip()
            return self._run_script(
                "25_build_name_candidates.py",
                env_overrides=self._phase2_env(
                    context_key=context_key,
                    place_set_id=psid or None,
                ),
            )

        model_name = "phase2_name_ranker_v1"
        with _conn_ctx() as conn:
            psid = str(place_set_id or "").strip()
            if not psid:
                psid = str(self._latest_place_set_id(conn, context_key=context_key) or "").strip()
            if not psid:
                raise RuntimeError("No place_set_id found for Step 25")

            artifact = None
            if self._table_exists(conn, self.t_model_registry):
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT artifact
                        FROM {_q_ident(self.t_model_registry)}
                        WHERE model_name = %s
                        LIMIT 1
                        """,
                        (model_name,),
                    )
                    row = cur.fetchone() or {}
                    artifact = row.get("artifact") if isinstance(row, dict) else (row[0] if row else None)

            summary = build_for_place_set(
                conn,
                place_set_id=psid,
                ranker_artifact=(artifact if isinstance(artifact, dict) else None),
            ) or {}

            written_rows = 0
            if self._table_exists(conn, self.t_name_candidates):
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT COUNT(*)::int AS n
                        FROM {_q_ident(self.t_name_candidates)}
                        WHERE place_set_id::text = %s
                        """,
                        (psid,),
                    )
                    row = cur.fetchone() or {}
                    written_rows = int(row.get("n") if isinstance(row, dict) else (row[0] if row else 0))

        payload = {
            "place_set_id": psid,
            "places": int(summary.get("places") or 0),
            "name_candidates": int(summary.get("name_candidates") or 0),
            "written_rows": int(written_rows),
        }
        return {
            "ok": True,
            "returncode": 0,
            "stdout": json.dumps(payload),
            "stderr": "",
            "script": "25_build_name_candidates.py",
            "summary": payload,
        }

    def run_step_35_train_name_ranker(self, *, force: bool = False) -> Dict[str, Any]:
        refresh_plan = self.get_step35_refresh_plan()
        if not force and not bool(refresh_plan.get("should_run")):
            return {
                "ok": True,
                "returncode": 0,
                "stdout": "",
                "stderr": "",
                "script": "35_train_name_ranker.py",
                "skipped": True,
                "skip_reason": refresh_plan.get("reason"),
                "refresh_plan": refresh_plan,
            }
        out = self._run_script("35_train_name_ranker.py")
        out["skipped"] = False
        out["refresh_plan"] = refresh_plan
        return out

    def get_geo_context_summary(self, *, context_key: Optional[str] = None, sample_limit: int = 20) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "table_exists": False,
            "rows_total": 0,
            "rows_for_latest_run": 0,
            "latest_extract_run_id": None,
            "latest_context_key": None,
            "recent_runs": [],
            "sample_rows": [],
        }
        with _conn_ctx() as conn:
            if not self._table_exists(conn, self.t_node_geo_context):
                return out

            out["table_exists"] = True

            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*)::int AS n FROM {_q_ident(self.t_node_geo_context)}")
                row_total = cur.fetchone() or {}
                out["rows_total"] = int(row_total.get("n") or 0)

                cur.execute(
                    f"""
                    SELECT
                      extract_run_id::text AS extract_run_id,
                      context_key,
                      COUNT(*)::int AS n_rows,
                      MAX(created_at) AS last_created_at
                    FROM {_q_ident(self.t_node_geo_context)}
                    GROUP BY 1,2
                    ORDER BY last_created_at DESC NULLS LAST
                    LIMIT 10
                    """
                )
                out["recent_runs"] = list(cur.fetchall() or [])

                if context_key:
                    cur.execute(
                        """
                        SELECT extract_run_id::text AS extract_run_id, context_key
                        FROM geo_raw.extract_runs
                        WHERE context_key = %s
                        ORDER BY extracted_at DESC
                        LIMIT 1
                        """,
                        (context_key,),
                    )
                else:
                    cur.execute(
                        """
                        SELECT extract_run_id::text AS extract_run_id, context_key
                        FROM geo_raw.extract_runs
                        ORDER BY extracted_at DESC
                        LIMIT 1
                        """
                    )
                latest = cur.fetchone() or {}
                latest_extract_run_id = latest.get("extract_run_id")
                out["latest_extract_run_id"] = latest_extract_run_id
                out["latest_context_key"] = latest.get("context_key")

                if latest_extract_run_id:
                    cur.execute(
                        f"""
                        SELECT COUNT(*)::int AS n
                        FROM {_q_ident(self.t_node_geo_context)}
                        WHERE extract_run_id::text = %s
                        """,
                        (latest_extract_run_id,),
                    )
                    row_latest = cur.fetchone() or {}
                    out["rows_for_latest_run"] = int(row_latest.get("n") or 0)

                    cur.execute(
                        f"""
                        SELECT
                          node_id::text AS node_id,
                          geohash7,
                          lat,
                          lon,
                          transit_density_300m,
                          poi_density_300m,
                          tag_stop_weight,
                          tag_poi_weight,
                          created_at
                        FROM {_q_ident(self.t_node_geo_context)}
                        WHERE extract_run_id::text = %s
                        ORDER BY created_at DESC NULLS LAST
                        LIMIT %s
                        """,
                        (latest_extract_run_id, int(sample_limit)),
                    )
                    out["sample_rows"] = list(cur.fetchall() or [])

        return out

    def get_step35_training_summary(self, *, place_set_id: Optional[str] = None) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name_feedback_count": 0,
            "type_feedback_count": 0,
            "name_candidates_scored": 0,
            "name_candidates_total": 0,
            "place_type_scored": 0,
            "place_candidates_total": 0,
            "recent_models": [],
        }
        with _conn_ctx() as conn:
            if self._table_exists(conn, self.t_name_feedback):
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT COUNT(*)::int AS n
                        FROM {_q_ident(self.t_name_feedback)}
                        WHERE (%s IS NULL OR place_set_id::text = %s)
                        """,
                        (place_set_id, place_set_id),
                    )
                    out["name_feedback_count"] = int((cur.fetchone() or {}).get("n") or 0)

            if self._table_exists(conn, self.t_poi_stop_feedback):
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT COUNT(*)::int AS n
                        FROM {_q_ident(self.t_poi_stop_feedback)}
                        WHERE (%s IS NULL OR place_set_id::text = %s)
                        """,
                        (place_set_id, place_set_id),
                    )
                    out["type_feedback_count"] = int((cur.fetchone() or {}).get("n") or 0)

            if self._table_exists(conn, self.t_name_candidates):
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT
                          COUNT(*)::int AS n_total,
                          COUNT(*) FILTER (WHERE model_score IS NOT NULL)::int AS n_scored
                        FROM {_q_ident(self.t_name_candidates)}
                        WHERE (%s IS NULL OR place_set_id::text = %s)
                        """,
                        (place_set_id, place_set_id),
                    )
                    r = cur.fetchone() or {}
                    out["name_candidates_total"] = int(r.get("n_total") or 0)
                    out["name_candidates_scored"] = int(r.get("n_scored") or 0)

            if self._table_exists(conn, self.t_place_candidates):
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT
                          COUNT(*)::int AS n_total,
                          COUNT(*) FILTER (WHERE model_place_type IS NOT NULL)::int AS n_scored
                        FROM {_q_ident(self.t_place_candidates)}
                        WHERE (%s IS NULL OR place_set_id::text = %s)
                        """,
                        (place_set_id, place_set_id),
                    )
                    r = cur.fetchone() or {}
                    out["place_candidates_total"] = int(r.get("n_total") or 0)
                    out["place_type_scored"] = int(r.get("n_scored") or 0)

            if self._table_exists(conn, self.t_model_registry):
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT
                          model_name,
                          version,
                          updated_at
                        FROM {_q_ident(self.t_model_registry)}
                        ORDER BY updated_at DESC NULLS LAST
                        LIMIT 20
                        """
                    )
                    out["recent_models"] = list(cur.fetchall() or [])

        return out

    def get_step35_quality_rows(self, *, place_set_id: Optional[str] = None) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name_rows": [],
            "type_rows": [],
        }
        with _conn_ctx() as conn:
            if self._table_exists(conn, self.t_name_feedback) and self._table_exists(conn, self.t_name_candidates):
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        WITH latest_name_feedback AS (
                          SELECT DISTINCT ON (f.place_set_id, f.place_candidate_id)
                            f.place_set_id::text AS place_set_id,
                            f.place_candidate_id::text AS place_candidate_id,
                            f.chosen_name_candidate_id::text AS chosen_name_candidate_id,
                            f.chosen_name,
                            f.chosen_source,
                            f.created_at
                          FROM {_q_ident(self.t_name_feedback)} f
                          WHERE (%s IS NULL OR f.place_set_id::text = %s)
                          ORDER BY f.place_set_id, f.place_candidate_id, f.created_at DESC
                        )
                        SELECT
                          l.place_set_id,
                          l.place_candidate_id,
                          l.chosen_name_candidate_id,
                          l.chosen_name,
                          l.chosen_source,
                          l.created_at,
                          c.model_rank AS chosen_model_rank,
                          CASE WHEN c.model_rank = 1 THEN true ELSE false END AS chosen_is_top1
                        FROM latest_name_feedback l
                        LEFT JOIN {_q_ident(self.t_name_candidates)} c
                          ON c.name_candidate_id::text = l.chosen_name_candidate_id
                        ORDER BY l.created_at DESC NULLS LAST
                        """,
                        (place_set_id, place_set_id),
                    )
                    out["name_rows"] = list(cur.fetchall() or [])

            if self._table_exists(conn, self.t_poi_stop_feedback) and self._table_exists(conn, self.t_place_candidates):
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        WITH latest_type_feedback AS (
                          SELECT DISTINCT ON (f.place_set_id, f.place_candidate_id)
                            f.place_set_id::text AS place_set_id,
                            f.place_candidate_id::text AS place_candidate_id,
                            UPPER(f.chosen_place_type) AS true_label,
                            f.created_at
                          FROM {_q_ident(self.t_poi_stop_feedback)} f
                          WHERE (%s IS NULL OR f.place_set_id::text = %s)
                          ORDER BY f.place_set_id, f.place_candidate_id, f.created_at DESC
                        )
                        SELECT
                          l.place_set_id,
                          l.place_candidate_id,
                          l.true_label,
                          UPPER(pc.model_place_type) AS pred_label,
                          pc.model_place_type_score AS pred_score,
                          l.created_at
                        FROM latest_type_feedback l
                        LEFT JOIN {_q_ident(self.t_place_candidates)} pc
                          ON pc.place_candidate_id::text = l.place_candidate_id
                        ORDER BY l.created_at DESC NULLS LAST
                        """,
                        (place_set_id, place_set_id),
                    )
                    out["type_rows"] = list(cur.fetchall() or [])

        return out

    def run_step_40_build_embeddings(self, *, force: bool = False) -> Dict[str, Any]:
        refresh_plan = self.get_step40_refresh_plan()
        if not force and not bool(refresh_plan.get("should_run")):
            return {
                "ok": True,
                "returncode": 0,
                "stdout": "",
                "stderr": "",
                "script": "40_build_embeddings.py",
                "skipped": True,
                "skip_reason": refresh_plan.get("reason"),
                "refresh_plan": refresh_plan,
            }
        out = self._run_script("40_build_embeddings.py")
        out["skipped"] = False
        out["refresh_plan"] = refresh_plan
        return out

    def run_step_50_reindex_opensearch(
        self,
        *,
        force: bool = False,
        embeddings_step_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        emb_result = dict(embeddings_step_result or {})
        if not force and emb_result.get("skipped"):
            return {
                "ok": True,
                "returncode": 0,
                "stdout": "",
                "stderr": "",
                "script": "50_reindex_opensearch.py",
                "skipped": True,
                "skip_reason": f"embeddings_step_skipped:{str(emb_result.get('skip_reason') or 'up_to_date')}",
                "upstream_step": "40_build_embeddings",
            }
        out = self._run_script("50_reindex_opensearch.py")
        out["skipped"] = False
        return out

    def run_step_60_search_demo(self, query_text: str) -> Dict[str, Any]:
        q = (query_text or "").strip()
        if not q:
            raise RuntimeError("query_text is required for Step 60.")
        return self._run_script("60_search_demo.py", args=[q])

    # -----------------------------
    # Core introspection helpers
    # -----------------------------
    def _table_exists(self, conn, table: str) -> bool:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (table,))
            row = cur.fetchone()
            return bool(row and row.get("ok"))

    def _table_columns(self, conn, table: str) -> set[str]:
        if "." in table:
            schema, name = table.split(".", 1)
        else:
            schema, name = "public", table
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                """,
                (schema, name),
            )
            rows = cur.fetchall() or []
        return {r["column_name"] for r in rows}

    def _count_table(self, conn, table: str) -> int:
        if not self._table_exists(conn, table):
            return 0
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*)::int AS n FROM {_q_ident(table)}")
            row = cur.fetchone()
            return int(row["n"]) if row and "n" in row else 0

    def _bbox_from_box2d_text(self, box: Optional[str]) -> Optional[List[float]]:
        """
        Parse 'BOX(minx miny,maxx maxy)' into [south, west, north, east]
        """
        if not box:
            return None
        m = re.match(
            r"BOX\(\s*([-\d.]+)\s+([-\d.]+)\s*,\s*([-\d.]+)\s+([-\d.]+)\s*\)",
            str(box),
        )
        if not m:
            return None
        minx, miny, maxx, maxy = map(float, m.groups())
        return [miny, minx, maxy, maxx]


    def _vector_literal(self, values: List[float]) -> str:
        # pgvector input format
        return "[" + ",".join(f"{float(v):.8f}" for v in values) + "]"
    def _parse_pgvector(self, v: Any) -> Optional[List[float]]:
        """
        pgvector might come back as:
        - list[float] / tuple[float]
        - string like '[0.1,0.2,...]' or '(0.1,0.2,...)'
        """
        if v is None:
            return None
        if isinstance(v, (list, tuple)):
            return [float(x) for x in v]
        if isinstance(v, str):
            s = v.strip()
            # strip [] or () if present
            if (s.startswith("[") and s.endswith("]")) or (s.startswith("(") and s.endswith(")")):
                s = s[1:-1].strip()
            if not s:
                return None
            parts = [p.strip() for p in s.split(",")]
            return [float(p) for p in parts if p]
        return None




    def sample_alias_embeddings(
        self,
        *,
        limit: int = 2000,
        region: Optional[str] = None,
        kind: Optional[str] = None,
        text_like: Optional[str] = None,
        table: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch embeddings for Global UMAP scatter.
        Returns rows with: alias_id, place_id, kind, region, text, embedding(list[float])
        """
        out: List[Dict[str, Any]] = []
        with _conn_ctx() as conn:
            use_canonical = (
                table is None
                and self._table_exists(conn, "geo_prod.place_alias_embeddings")
                and self._table_exists(conn, "geo_prod.place_aliases")
            )

            where = []
            params: List[Any] = []

            if use_canonical:
                if region is not None:
                    where.append("p.region = %s")
                    params.append(region)
                if kind is not None:
                    where.append("a.alias_kind = %s")
                    params.append(kind)
                if text_like:
                    where.append("a.alias ILIKE %s")
                    params.append(f"%{text_like}%")

                where_sql = ("WHERE " + " AND ".join(where)) if where else ""
                sql = f"""
                SELECT
                e.alias_id::text AS alias_id,
                e.place_id::text AS place_id,
                a.alias_kind AS kind,
                p.region AS region,
                a.alias AS text,
                e.embedding
                FROM geo_prod.place_alias_embeddings e
                JOIN geo_prod.place_aliases a
                  ON a.alias_id = e.alias_id
                LEFT JOIN geo_prod.places p
                  ON p.place_id = e.place_id
                {where_sql}
                LIMIT %s
                """
            else:
                t = table or getattr(self, "t_alias_vectors", None) or DEFAULT_T_ALIAS_VECTORS
                if region is not None:
                    where.append("region = %s")
                    params.append(region)
                if kind is not None:
                    where.append("kind = %s")
                    params.append(kind)
                if text_like:
                    where.append("text ILIKE %s")
                    params.append(f"%{text_like}%")

                where_sql = ("WHERE " + " AND ".join(where)) if where else ""
                sql = f"""
                SELECT
                id::text AS alias_id,
                place_id::text AS place_id,
                kind,
                region,
                text,
                embedding
                FROM {t}
                {where_sql}
                LIMIT %s
                """

            params.append(int(limit))
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = list(cur.fetchall() or [])

        for r in rows:
            emb = self._parse_pgvector(r.get("embedding"))
            if emb is None:
                continue
            r = dict(r)
            r["embedding"] = emb
            out.append(r)

        return out

    # -----------------------------
    # Selection learning (read selection_log)
        # -----------------------------
    def list_selection_events(
            self,
            *,
            phase: int = 2,
            object_type: str = "places",
            context_key: Optional[str] = None,
            limit: int = 500,
        ) -> List[Dict[str, Any]]:
            """
            Read selection history from geo_work.selection_log.

            Returns rows you can plot:
            selection_id, created_at, chosen_set_id, rejected_set_ids, params, metrics, context
            """

            limit = int(limit)

            with _conn_ctx() as conn:
                if not self._table_exists(conn, self.t_selection_log):
                    return []

                cols = self._table_columns(conn, self.t_selection_log)

                # Common column names (handle variants safely)
                id_col = "selection_id" if "selection_id" in cols else ("id" if "id" in cols else None)

                ts_col = None
                for c in ("selected_at", "created_at", "decided_at", "inserted_at", "ts", "timestamp"):
                    if c in cols:
                        ts_col = c
                        break

                phase_col = "phase" if "phase" in cols else None
                obj_col = "object_type" if "object_type" in cols else None

                chosen_col = "chosen_set_id" if "chosen_set_id" in cols else None
                rejected_col = "rejected_set_ids" if "rejected_set_ids" in cols else None

                params_col = "params" if "params" in cols else None
                metrics_col = "metrics" if "metrics" in cols else None
                context_col = "context" if "context" in cols else None

                select_parts = []
                select_parts.append(f"{id_col}::text AS selection_id" if id_col else "NULL::text AS selection_id")
                select_parts.append(f"{ts_col} AS created_at" if ts_col else "NULL::timestamptz AS created_at")
                select_parts.append(f"{chosen_col}::text AS chosen_set_id" if chosen_col else "NULL::text AS chosen_set_id")
                select_parts.append(f"{rejected_col} AS rejected_set_ids" if rejected_col else "NULL::uuid[] AS rejected_set_ids")
                select_parts.append(f"{params_col} AS params" if params_col else "NULL::jsonb AS params")
                select_parts.append(f"{metrics_col} AS metrics" if metrics_col else "NULL::jsonb AS metrics")
                select_parts.append(f"{context_col} AS context" if context_col else "NULL::jsonb AS context")

                where = []
                params: List[Any] = []

                if phase_col:
                    where.append(f"{phase_col} = %s")
                    params.append(int(phase))

                if obj_col:
                    where.append(f"{obj_col} = %s")
                    params.append(str(object_type))

                # context_key filter (you store it inside context JSON)
                if context_key and context_col:
                    where.append(f"({context_col} ->> 'context_key') = %s")
                    params.append(context_key)

                where_sql = ("WHERE " + " AND ".join(where)) if where else ""
                order_sql = f"ORDER BY {ts_col} DESC NULLS LAST" if ts_col else ""

                sql = f"""
                    SELECT {", ".join(select_parts)}
                    FROM {_q_ident(self.t_selection_log)}
                    {where_sql}
                    {order_sql}
                    LIMIT %s
                """
                params.append(limit)

                with conn.cursor() as cur:
                    cur.execute(sql, tuple(params))
                    rows = list(cur.fetchall() or [])

            # normalize rejected_set_ids to list[str]
            for r in rows:
                rej = r.get("rejected_set_ids")
                if rej is None:
                    r["rejected_set_ids"] = []
                else:
                    r["rejected_set_ids"] = [str(x) for x in list(rej)]
            return rows

    def semantic_search_aliases(
        self,
        *,
        query_text: str,
        top_k: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Hybrid geocoder search in PostgreSQL:
        - semantic kNN (pgvector)
        - trigram typo matching
        - lexical FTS
        - exact normalized match bonus
        """
        q = (query_text or "").strip()
        if not q:
            return []
        # The model package is optional for console startup and is only needed
        # when the operator explicitly requests semantic search.
        from phase2_semantics.src.pipeline.embeddings.embedder import embed

        k = int(top_k or SEARCH_TOP_K)
        vector = embed(q)
        vec_lit = self._vector_literal(vector)
        sem_limit = max(50, k * 20)
        lex_limit = max(50, k * 20)
        out: List[Dict[str, Any]] = []
        with _conn_ctx() as conn:
            if not self._table_exists(conn, "geo_prod.place_alias_embeddings"):
                raise RuntimeError("Missing table geo_prod.place_alias_embeddings. Run Step 40 first.")
            if not self._table_exists(conn, "geo_prod.place_aliases"):
                raise RuntimeError("Missing table geo_prod.place_aliases.")
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        """
                    WITH params AS (
                      SELECT
                        lower(trim(%s::text)) AS q_norm,
                        ('%%' || lower(trim(%s::text)) || '%%') AS q_like,
                        %s::vector AS q_vec
                    ),
                    sem AS (
                      SELECT
                        e.alias_id,
                        (1.0 - (e.embedding <=> p.q_vec))::float8 AS semantic_score
                      FROM geo_prod.place_alias_embeddings e
                      CROSS JOIN params p
                      ORDER BY (e.embedding <=> p.q_vec) ASC
                      LIMIT %s
                    ),
                    lex AS (
                      SELECT
                        a.alias_id,
                        similarity(a.normalized_alias, p.q_norm)::float8 AS trgm_score,
                        ts_rank_cd(
                          to_tsvector('simple', a.normalized_alias),
                          plainto_tsquery('simple', p.q_norm)
                        )::float8 AS fts_score
                      FROM geo_prod.place_aliases a
                      CROSS JOIN params p
                      WHERE
                        a.normalized_alias % p.q_norm
                        OR to_tsvector('simple', a.normalized_alias) @@ plainto_tsquery('simple', p.q_norm)
                        OR a.normalized_alias LIKE p.q_like
                      ORDER BY GREATEST(
                        similarity(a.normalized_alias, p.q_norm),
                        ts_rank_cd(to_tsvector('simple', a.normalized_alias), plainto_tsquery('simple', p.q_norm))
                      ) DESC
                      LIMIT %s
                    ),
                    candidates AS (
                      SELECT alias_id FROM sem
                      UNION
                      SELECT alias_id FROM lex
                    )
                    SELECT
                      a.alias_id::text AS alias_id,
                      e.place_id::text AS place_id,
                      a.alias AS alias_text,
                      a.lang,
                      a.alias_kind AS kind,
                      COALESCE(l.trgm_score, 0.0)::float8 AS trgm_score,
                      COALESCE(l.fts_score, 0.0)::float8 AS fts_score,
                      COALESCE(s.semantic_score, (1.0 - (e.embedding <=> p.q_vec)))::float8 AS semantic_score,
                      CASE WHEN a.normalized_alias = p.q_norm THEN 1.0 ELSE 0.0 END AS exact_bonus,
                      (
                        (0.42 * COALESCE(s.semantic_score, (1.0 - (e.embedding <=> p.q_vec))))
                        + (0.33 * COALESCE(l.trgm_score, 0.0))
                        + (0.20 * COALESCE(l.fts_score, 0.0))
                        + (0.05 * CASE WHEN a.normalized_alias = p.q_norm THEN 1.0 ELSE 0.0 END)
                      )::float8 AS score
                    FROM candidates c
                    JOIN geo_prod.place_aliases a
                      ON a.alias_id = c.alias_id
                    JOIN geo_prod.place_alias_embeddings e
                      ON a.alias_id = e.alias_id
                    CROSS JOIN params p
                    LEFT JOIN sem s
                      ON s.alias_id = c.alias_id
                    LEFT JOIN lex l
                      ON l.alias_id = c.alias_id
                    ORDER BY score DESC, semantic_score DESC
                    LIMIT %s
                    """,
                        (q, q, vec_lit, sem_limit, lex_limit, k),
                    )
                except Exception:
                    # Fallback if pg_trgm/fts index path is not yet available.
                    cur.execute(
                        """
                        WITH q AS (SELECT %s::vector AS v)
                        SELECT
                          a.alias_id::text AS alias_id,
                          e.place_id::text AS place_id,
                          a.alias AS alias_text,
                          a.lang,
                          a.alias_kind AS kind,
                          0.0::float8 AS trgm_score,
                          0.0::float8 AS fts_score,
                          (1.0 - (e.embedding <=> q.v))::float8 AS semantic_score,
                          0.0::float8 AS exact_bonus,
                          (1.0 - (e.embedding <=> q.v))::float8 AS score
                        FROM geo_prod.place_alias_embeddings e
                        JOIN geo_prod.place_aliases a
                          ON a.alias_id = e.alias_id
                        CROSS JOIN q
                        ORDER BY (e.embedding <=> q.v) ASC
                        LIMIT %s
                        """,
                        (vec_lit, k),
                    )
                out = list(cur.fetchall() or [])
        return out

    def _fetch_prod_places_basic(self, conn, place_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Fetch canonical_name/place_type for place_ids from geo_prod.places.
        Returns dict keyed by place_id string.
        """
        if not place_ids:
            return {}

        arr = self._uuid_array_literal(place_ids)

        sql = """
        SELECT
        place_id::text AS place_id,
        canonical_name,
        place_type
        FROM geo_prod.places
        WHERE place_id = ANY(%s::uuid[])
        """
        with conn.cursor() as cur:
            cur.execute(sql, (arr,))
            rows = list(cur.fetchall() or [])

        return {r["place_id"]: dict(r) for r in rows}

    def semantic_search_places(
        self,
        *,
        query_text: str,
        top_k: Optional[int] = None,
        dedupe_by_place: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        pgvector alias search -> (optional) dedupe by place_id -> enrich with geo_prod.places.
        """
        hits = self.semantic_search_aliases(query_text=query_text, top_k=top_k)

        if not dedupe_by_place:
            return hits

        # keep best hit per place_id
        best: Dict[str, Dict[str, Any]] = {}
        for h in hits:
            pid = h.get("place_id") or ""
            if not pid:
                continue
            if (pid not in best) or (h["score"] > best[pid]["score"]):
                best[pid] = h

        place_ids = list(best.keys())

        with _conn_ctx() as conn:
            info = self._fetch_prod_places_basic(conn, place_ids)

        out: List[Dict[str, Any]] = []
        for pid in place_ids:
            row = best[pid]
            prod = info.get(pid, {})
            out.append(
                {
                    **row,
                    "canonical_name": prod.get("canonical_name"),
                    "place_type": prod.get("place_type"),
                }
            )

        # order by score desc
        out.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
        return out


    def _geo_types_list(self, types: Optional[Any]) -> Optional[List[str]]:
        if types is None:
            return None
        vals: List[str] = []
        if isinstance(types, str):
            raw = [x.strip() for x in types.split(",")]
        elif isinstance(types, (list, tuple, set)):
            raw = [str(x).strip() for x in types]
        else:
            return None

        for v in raw:
            if not v:
                continue
            t = v.upper()
            if t == "ADDRESS":
                t = "OTHER"
            vals.append(t)
        return vals or None

    def _geo_bbox(self, bbox: Optional[Any]) -> Optional[Tuple[float, float, float, float]]:
        if bbox is None:
            return None
        parts: List[str]
        if isinstance(bbox, str):
            parts = [p.strip() for p in bbox.split(",")]
        elif isinstance(bbox, (list, tuple)):
            parts = [str(p).strip() for p in bbox]
        else:
            return None
        if len(parts) != 4:
            return None
        try:
            s, w, n, e = [float(x) for x in parts]
        except Exception:
            return None
        if s > n:
            s, n = n, s
        if w > e:
            w, e = e, w
        return (s, w, n, e)

    def _geo_in_bbox(self, lat: Optional[float], lon: Optional[float], bbox: Optional[Tuple[float, float, float, float]]) -> bool:
        if bbox is None:
            return True
        if lat is None or lon is None:
            return False
        s, w, n, e = bbox
        return (s <= float(lat) <= n) and (w <= float(lon) <= e)

    def _fetch_geo_place_details(self, conn, place_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        if not place_ids:
            return {}

        arr = self._uuid_array_literal(place_ids)
        has_summary = self._table_exists(conn, "geo_prod.v_place_summary")
        has_points = self._table_exists(conn, "geo_prod.v_place_points")

        if has_summary:
            sql = """
            SELECT
              p.place_id::text AS place_id,
              p.canonical_name,
              p.place_type,
              p.region,
              ST_Y(s.center_geom)::float8 AS lat,
              ST_X(s.center_geom)::float8 AS lon
            FROM geo_prod.places p
            LEFT JOIN geo_prod.v_place_summary s
              ON s.place_id = p.place_id
            WHERE p.place_id = ANY(%s::uuid[])
            """
        elif has_points:
            sql = """
            SELECT
              p.place_id::text AS place_id,
              p.canonical_name,
              p.place_type,
              p.region,
              AVG(v.lat)::float8 AS lat,
              AVG(v.lon)::float8 AS lon
            FROM geo_prod.places p
            LEFT JOIN geo_prod.v_place_points v
              ON v.place_id = p.place_id
            WHERE p.place_id = ANY(%s::uuid[])
            GROUP BY p.place_id, p.canonical_name, p.place_type, p.region
            """
        else:
            sql = """
            SELECT
              p.place_id::text AS place_id,
              p.canonical_name,
              p.place_type,
              p.region,
              NULL::float8 AS lat,
              NULL::float8 AS lon
            FROM geo_prod.places p
            WHERE p.place_id = ANY(%s::uuid[])
            """

        with conn.cursor() as cur:
            cur.execute(sql, (arr,))
            rows = list(cur.fetchall() or [])
        return {str(r.get("place_id") or ""): dict(r) for r in rows if r.get("place_id")}

    def _build_geo_timing(self, *, t0: float, embed_ms: float, db_ms: float, rerank_ms: float) -> Dict[str, float]:
        return {
            "total_ms": round((perf_counter() - t0) * 1000.0, 3),
            "embed_ms": round(float(embed_ms), 3),
            "db_ms": round(float(db_ms), 3),
            "rerank_ms": round(float(rerank_ms), 3),
        }

    def _to_float_or_none(self, v: Any) -> Optional[float]:
        try:
            if v is None:
                return None
            return float(v)
        except Exception:
            return None

    def geo_api_health(self) -> Dict[str, Any]:
        t0 = perf_counter()
        try:
            with _conn_ctx() as conn:
                checks = {
                    "place_alias_embeddings": self._table_exists(conn, "geo_prod.place_alias_embeddings"),
                    "place_aliases": self._table_exists(conn, "geo_prod.place_aliases"),
                    "v_place_points": self._table_exists(conn, "geo_prod.v_place_points"),
                }
                queryable = False
                if checks["place_alias_embeddings"] and checks["place_aliases"]:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1 FROM geo_prod.place_alias_embeddings LIMIT 1")
                        queryable = bool(cur.fetchone())

            status = "Healthy" if (all(checks.values()) and queryable) else "Degraded"
            return {
                "status": status,
                "version": "Geo API v1",
                "queryable": bool(queryable),
                "checks": checks,
                "timings_ms": {
                    "total_ms": round((perf_counter() - t0) * 1000.0, 3),
                },
            }
        except Exception as e:
            return {
                "status": "Degraded",
                "version": "Geo API v1",
                "queryable": False,
                "checks": {},
                "timings_ms": {
                    "total_ms": round((perf_counter() - t0) * 1000.0, 3),
                },
                "error": str(e),
            }

    def geo_api_geocode(
    self,
    *,
    query_text: str,
    top_k: int = 10,
    bbox: Optional[Any] = None,
    area_key: Optional[str] = None,
    types: Optional[Any] = None,
    language: Optional[str] = None,
) -> Dict[str, Any]:
        q = str(query_text or "").strip()
        if not q:
            raise RuntimeError("q is required")

        k = max(1, int(top_k or 10))
        bbox_parsed = self._geo_bbox(bbox)
        type_filter = self._geo_types_list(types)
        area_norm = str(area_key or "").strip().lower() or None

        t0 = perf_counter()
        db_ms = 0.0
        rerank_ms = 0.0

        t_search = perf_counter()
        alias_hits = self.semantic_search_aliases(query_text=q, top_k=max(50, k * 20))
        db_ms += (perf_counter() - t_search) * 1000.0

        t_rerank = perf_counter()
        best: Dict[str, Dict[str, Any]] = {}
        for row in alias_hits:
            pid = str(row.get("place_id") or "").strip()
            if not pid:
                continue

            semantic_part = 0.42 * float(row.get("semantic_score") or 0.0)
            lexical_part = (0.33 * float(row.get("trgm_score") or 0.0)) + (0.20 * float(row.get("fts_score") or 0.0))
            prior_part = 0.05 * float(row.get("exact_bonus") or 0.0)
            score = float(semantic_part + lexical_part + prior_part)

            cand = {
                "place_id": pid,
                "alias_text": row.get("alias_text"),
                "lang": row.get("lang"),
                "kind": row.get("kind"),
                "score": score,
                "score_parts": {
                    "semantic": float(semantic_part),
                    "lexical": float(lexical_part),
                    "prior": float(prior_part),
                },
            }
            if (pid not in best) or (float(cand["score"]) > float(best[pid].get("score") or 0.0)):
                best[pid] = cand
        rerank_ms += (perf_counter() - t_rerank) * 1000.0

        place_ids = list(best.keys())
        t_info = perf_counter()
        with _conn_ctx() as conn:
            details = self._fetch_geo_place_details(conn, place_ids)
        db_ms += (perf_counter() - t_info) * 1000.0

        t_rerank = perf_counter()
        out_candidates: List[Dict[str, Any]] = []
        for pid, row in best.items():
            d = details.get(pid, {})
            entity_type = str(d.get("place_type") or "OTHER")
            region = str(d.get("region") or "")
            lat = self._to_float_or_none(d.get("lat"))
            lon = self._to_float_or_none(d.get("lon"))

            if type_filter and entity_type.upper() not in type_filter:
                continue
            if area_norm and region.lower() != area_norm:
                continue
            if not self._geo_in_bbox(lat, lon, bbox_parsed):
                continue

            out_candidates.append(
                {
                    "id": pid,
                    "name": d.get("canonical_name") or row.get("alias_text") or pid,
                    "entity_type": entity_type,
                    "lat": lat,
                    "lon": lon,
                    "score": float(row.get("score") or 0.0),
                    "score_parts": row.get("score_parts") or {"semantic": 0.0, "lexical": 0.0, "prior": 0.0},
                    "source": "phase2_vector",
                    "alias_text": row.get("alias_text"),
                    "lang": row.get("lang") or language,
                    "region": region,
                }
            )

        out_candidates.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
        out_candidates = out_candidates[:k]
        rerank_ms += (perf_counter() - t_rerank) * 1000.0

        return {
            "query": q,
            "endpoint": "geocode",
            "timings_ms": self._build_geo_timing(t0=t0, embed_ms=0.0, db_ms=db_ms, rerank_ms=rerank_ms),
            "candidates": out_candidates,
        }

    def geo_api_autocomplete(
    self,
    *,
    query_text: str,
    top_k: int = 10,
    bbox: Optional[Any] = None,
    area_key: Optional[str] = None,
    types: Optional[Any] = None,
    language: Optional[str] = None,
) -> Dict[str, Any]:
        q = str(query_text or "").strip()
        if not q:
            raise RuntimeError("q is required")

        q_norm = q.lower()
        k = max(1, int(top_k or 10))
        bbox_parsed = self._geo_bbox(bbox)
        type_filter = self._geo_types_list(types)
        area_norm = str(area_key or "").strip().lower() or None

        t0 = perf_counter()
        db_ms = 0.0
        rerank_ms = 0.0

        t_search = perf_counter()
        alias_hits = self.semantic_search_aliases(query_text=q, top_k=max(80, k * 25))
        db_ms += (perf_counter() - t_search) * 1000.0

        t_rerank = perf_counter()
        best: Dict[str, Dict[str, Any]] = {}
        for row in alias_hits:
            pid = str(row.get("place_id") or "").strip()
            if not pid:
                continue

            alias_txt = str(row.get("alias_text") or "").strip().lower()
            prefix_bonus = 0.0
            if alias_txt and q_norm:
                if alias_txt.startswith(q_norm):
                    prefix_bonus = 0.20
                elif q_norm in alias_txt:
                    prefix_bonus = 0.10

            semantic_part = 0.28 * float(row.get("semantic_score") or 0.0)
            lexical_part = (
                (0.42 * float(row.get("trgm_score") or 0.0))
                + (0.20 * float(row.get("fts_score") or 0.0))
                + float(prefix_bonus)
            )
            prior_part = 0.10 * float(row.get("exact_bonus") or 0.0)
            score = float(semantic_part + lexical_part + prior_part)

            cand = {
                "place_id": pid,
                "alias_text": row.get("alias_text"),
                "lang": row.get("lang"),
                "kind": row.get("kind"),
                "score": score,
                "score_parts": {
                    "semantic": float(semantic_part),
                    "lexical": float(lexical_part),
                    "prior": float(prior_part),
                },
            }
            if (pid not in best) or (float(cand["score"]) > float(best[pid].get("score") or 0.0)):
                best[pid] = cand
        rerank_ms += (perf_counter() - t_rerank) * 1000.0

        place_ids = list(best.keys())
        t_info = perf_counter()
        with _conn_ctx() as conn:
            details = self._fetch_geo_place_details(conn, place_ids)
        db_ms += (perf_counter() - t_info) * 1000.0

        t_rerank = perf_counter()
        out_candidates: List[Dict[str, Any]] = []
        for pid, row in best.items():
            d = details.get(pid, {})
            entity_type = str(d.get("place_type") or "OTHER")
            region = str(d.get("region") or "")
            lat = self._to_float_or_none(d.get("lat"))
            lon = self._to_float_or_none(d.get("lon"))

            if type_filter and entity_type.upper() not in type_filter:
                continue
            if area_norm and region.lower() != area_norm:
                continue
            if not self._geo_in_bbox(lat, lon, bbox_parsed):
                continue

            out_candidates.append(
                {
                    "id": pid,
                    "name": d.get("canonical_name") or row.get("alias_text") or pid,
                    "entity_type": entity_type,
                    "lat": lat,
                    "lon": lon,
                    "score": float(row.get("score") or 0.0),
                    "score_parts": row.get("score_parts") or {"semantic": 0.0, "lexical": 0.0, "prior": 0.0},
                    "source": "phase2_vector",
                    "alias_text": row.get("alias_text"),
                    "lang": row.get("lang") or language,
                    "region": region,
                }
            )

        out_candidates.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
        out_candidates = out_candidates[:k]
        rerank_ms += (perf_counter() - t_rerank) * 1000.0

        return {
            "query": q,
            "endpoint": "autocomplete",
            "timings_ms": self._build_geo_timing(t0=t0, embed_ms=0.0, db_ms=db_ms, rerank_ms=rerank_ms),
            "candidates": out_candidates,
        }

    def geo_api_reverse(
    self,
    *,
    lat: float,
    lon: float,
    top_k: int = 10,
    radius_m: float = 1200.0,
    types: Optional[Any] = None,
    area_key: Optional[str] = None,
    query_text: Optional[str] = None,
) -> Dict[str, Any]:
        k = max(1, int(top_k or 10))
        radius = float(max(50.0, min(float(radius_m or 1200.0), 20000.0)))
        type_filter = self._geo_types_list(types)
        area_norm = str(area_key or "").strip().lower() or None

        t0 = perf_counter()
        db_ms = 0.0
        rerank_ms = 0.0

        t_spatial = perf_counter()
        with _conn_ctx() as conn:
            if not self._table_exists(conn, "geo_prod.v_place_points"):
                raise RuntimeError("Missing view geo_prod.v_place_points")

            sql = """
            SELECT
              p.place_id::text AS place_id,
              p.canonical_name,
              p.place_type,
              p.region,
              AVG(v.lat)::float8 AS lat,
              AVG(v.lon)::float8 AS lon,
              MIN(
                ST_Distance(
                  v.geom::geography,
                  ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
                )
              )::float8 AS distance_m,
              AVG(v.confidence)::float8 AS avg_confidence
            FROM geo_prod.v_place_points v
            JOIN geo_prod.places p
              ON p.place_id = v.place_id
            WHERE ST_DWithin(
              v.geom::geography,
              ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
              %s
            )
            AND (%s::text[] IS NULL OR p.place_type = ANY(%s::text[]))
            AND (%s IS NULL OR lower(COALESCE(p.region, '')) = lower(%s))
            GROUP BY p.place_id, p.canonical_name, p.place_type, p.region
            ORDER BY distance_m ASC, avg_confidence DESC
            LIMIT %s
            """
            with conn.cursor() as cur:
                cur.execute(
                    sql,
                    (
                        float(lon),
                        float(lat),
                        float(lon),
                        float(lat),
                        radius,
                        type_filter,
                        type_filter,
                        area_norm,
                        area_norm,
                        max(50, k * 8),
                    ),
                )
                rows = list(cur.fetchall() or [])
        db_ms += (perf_counter() - t_spatial) * 1000.0

        semantic_by_place: Dict[str, float] = {}
        q = str(query_text or "").strip()
        if q and rows:
            t_sem = perf_counter()
            sem_hits = self.semantic_search_aliases(query_text=q, top_k=max(100, len(rows) * 6))
            db_ms += (perf_counter() - t_sem) * 1000.0
            for r in sem_hits:
                pid = str(r.get("place_id") or "").strip()
                if not pid:
                    continue
                s = float(r.get("semantic_score") or 0.0)
                if pid not in semantic_by_place or s > semantic_by_place[pid]:
                    semantic_by_place[pid] = s

        t_rank = perf_counter()
        ranked: List[Dict[str, Any]] = []
        for r in rows:
            pid = str(r.get("place_id") or "")
            d_m = float(r.get("distance_m") or 999999.0)
            proximity = max(0.0, 1.0 - (d_m / radius))
            conf = max(0.0, min(float(r.get("avg_confidence") or 0.0), 1.0))
            sem_tie = float(semantic_by_place.get(pid) or 0.0)

            semantic_part = 0.15 * sem_tie
            lexical_part = 0.0
            prior_part = (0.80 * proximity) + (0.05 * conf)
            score = float(semantic_part + lexical_part + prior_part)

            ranked.append(
                {
                    "id": pid,
                    "name": r.get("canonical_name") or pid,
                    "entity_type": r.get("place_type") or "OTHER",
                    "lat": self._to_float_or_none(r.get("lat")),
                    "lon": self._to_float_or_none(r.get("lon")),
                    "score": score,
                    "score_parts": {
                        "semantic": float(semantic_part),
                        "lexical": float(lexical_part),
                        "prior": float(prior_part),
                    },
                    "source": "phase2_vector",
                    "distance_m": float(d_m),
                    "avg_confidence": float(conf),
                    "region": r.get("region"),
                }
            )

        ranked.sort(
            key=lambda x: (
                float(x.get("distance_m") or 999999.0),
                -float((x.get("score_parts") or {}).get("semantic") or 0.0),
                -float(x.get("avg_confidence") or 0.0),
            )
        )
        ranked = ranked[:k]
        rerank_ms += (perf_counter() - t_rank) * 1000.0

        return {
            "query": q,
            "endpoint": "reverse",
            "timings_ms": self._build_geo_timing(t0=t0, embed_ms=0.0, db_ms=db_ms, rerank_ms=rerank_ms),
            "candidates": ranked,
        }

        # -----------------------------
        # Set listing / tables
        # -----------------------------
    def list_candidate_sets(self, *, context_key: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
            limit = int(limit)
            with _conn_ctx() as conn:
                cols = self._table_columns(conn, self.t_place_sets)

                id_col = "place_set_id" if "place_set_id" in cols else ("candidate_set_id" if "candidate_set_id" in cols else None)
                if not id_col:
                    raise RuntimeError(f"Cannot find set id column in {self.t_place_sets}. Columns={sorted(cols)}")

                created_col = "created_at" if "created_at" in cols else ("extracted_at" if "extracted_at" in cols else None)
                rank_col = "rank_score" if "rank_score" in cols else ("score" if "score" in cols else None)
                src_run_col = "source_extract_run_id" if "source_extract_run_id" in cols else ("extract_run_id" if "extract_run_id" in cols else None)
                ctx_col = "context_key" if "context_key" in cols else None

                select_parts = [f"{id_col}::text AS place_set_id"]
                select_parts.append(f"{created_col} AS created_at" if created_col else "NULL::timestamptz AS created_at")
                select_parts.append(f"{rank_col} AS rank_score" if rank_col else "NULL::float8 AS rank_score")
                select_parts.append(f"{src_run_col}::text AS source_extract_run_id" if src_run_col else "NULL::text AS source_extract_run_id")
                select_parts.append(f"{ctx_col} AS context_key" if ctx_col else "NULL::text AS context_key")

                where = ""
                params: List[Any] = []
                if context_key and ctx_col:
                    where = f"WHERE {ctx_col} = %s"
                    params.append(context_key)

                sql = f"""
                    SELECT {", ".join(select_parts)}
                    FROM {_q_ident(self.t_place_sets)}
                    {where}
                    ORDER BY created_at DESC NULLS LAST
                    LIMIT %s
                """
                params.append(limit)

                with conn.cursor() as cur:
                    cur.execute(sql, tuple(params))
                    return list(cur.fetchall() or [])

    def list_place_sets(self, *, context_key: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
            limit = int(limit)
            with _conn_ctx() as conn:
                cols_ps = self._table_columns(conn, self.t_place_sets)
                cols_pc = self._table_columns(conn, self.t_place_candidates)

                ps_id = "place_set_id" if "place_set_id" in cols_ps else ("candidate_set_id" if "candidate_set_id" in cols_ps else None)
                if not ps_id:
                    raise RuntimeError(f"Cannot find set id column in {self.t_place_sets}. Columns={sorted(cols_ps)}")

                ps_created = "created_at" if "created_at" in cols_ps else ("extracted_at" if "extracted_at" in cols_ps else None)
                ps_ctx = "context_key" if "context_key" in cols_ps else None
                ps_name = "name" if "name" in cols_ps else ("label" if "label" in cols_ps else None)
                ps_params = "params_used" if "params_used" in cols_ps else None

                pc_set = "place_set_id" if "place_set_id" in cols_pc else ("candidate_set_id" if "candidate_set_id" in cols_pc else None)
                if not pc_set:
                    raise RuntimeError(f"Cannot find set FK column in {self.t_place_candidates}. Columns={sorted(cols_pc)}")

                pc_id = "place_candidate_id" if "place_candidate_id" in cols_pc else None
                if not pc_id:
                    raise RuntimeError(f"Cannot find place_candidate_id in {self.t_place_candidates}. Columns={sorted(cols_pc)}")

                pc_geom = "center_geom" if "center_geom" in cols_pc else ("geom" if "geom" in cols_pc else None)

                name_expr = (
                    f"COALESCE(ps.{ps_name}, ('placeset ' || LEFT(ps.{ps_id}::text, 8)))"
                    if ps_name else
                    f"('placeset ' || LEFT(ps.{ps_id}::text, 8))"
                )
                created_expr = f"ps.{ps_created}" if ps_created else "NULL::timestamptz"
                ctx_expr = f"ps.{ps_ctx}" if ps_ctx else "NULL::text"
                params_expr = f"ps.{ps_params}" if ps_params else "NULL::jsonb"
                bbox_expr = f"ST_Extent(pc.{pc_geom})::text AS bbox" if pc_geom else "NULL::text AS bbox"

                where = ""
                params: List[Any] = []
                if context_key and ps_ctx:
                    where = f"WHERE ps.{ps_ctx} = %s"
                    params.append(context_key)

                sql = f"""
                    SELECT
                    ps.{ps_id}::text AS place_set_id,
                    {name_expr} AS name,
                    {ctx_expr} AS context_key,
                    {params_expr} AS params_used,
                    {created_expr} AS created_at,
                    COUNT(pc.{pc_id})::int AS n_candidates,
                    {bbox_expr}
                    FROM {_q_ident(self.t_place_sets)} ps
                    LEFT JOIN {_q_ident(self.t_place_candidates)} pc
                    ON pc.{pc_set} = ps.{ps_id}
                    {where}
                    GROUP BY ps.{ps_id}, {name_expr}, {ctx_expr}, {params_expr}, {created_expr}
                    ORDER BY created_at DESC NULLS LAST
                    LIMIT %s
                """
                params.append(limit)

                with conn.cursor() as cur:
                    cur.execute(sql, tuple(params))
                    rows = list(cur.fetchall() or [])

            for r in rows:
                r["bbox"] = self._bbox_from_box2d_text(r.get("bbox"))
            return rows

    def delete_place_set(
        self,
        place_set_id: str,
        *,
        delete_selection_logs: bool = True,
        delete_prod_entities: bool = False,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
            psid = str(place_set_id or "").strip()
            if not psid:
                raise RuntimeError("place_set_id is required")

            out: Dict[str, Any] = {
                "place_set_id": psid,
                "work_places": 0,
                "work_aliases": 0,
                "work_node_maps": 0,
                "name_candidates": 0,
                "name_feedback": 0,
                "type_feedback": 0,
                "selection_logs_as_chosen": 0,
                "selection_logs_with_rejected_ref": 0,
                "prod_places": 0,
                "prod_aliases": 0,
                "prod_node_maps": 0,
                "deleted_selection_logs": 0,
                "updated_selection_logs_arrays": 0,
                "deleted_prod_node_maps": 0,
                "deleted_prod_aliases": 0,
                "deleted_prod_places": 0,
                "deleted_place_set": 0,
                "dry_run": bool(dry_run),
            }

            with _conn_ctx() as conn:
                if not self._table_exists(conn, self.t_place_sets):
                    raise RuntimeError(f"Missing table: {self.t_place_sets}")

                cols_ps = self._table_columns(conn, self.t_place_sets)
                set_id_col = "place_set_id" if "place_set_id" in cols_ps else ("candidate_set_id" if "candidate_set_id" in cols_ps else None)
                if not set_id_col:
                    raise RuntimeError(f"Cannot find set id column in {self.t_place_sets}. Columns={sorted(cols_ps)}")

                place_ids: List[str] = []
                if self._table_exists(conn, self.t_place_candidates):
                    with conn.cursor() as cur:
                        cur.execute(
                            f"""
                            SELECT place_candidate_id::text AS place_id
                            FROM {_q_ident(self.t_place_candidates)}
                            WHERE place_set_id::text = %s
                            """,
                            (psid,),
                        )
                        place_ids = [str(r.get("place_id") or "") for r in (cur.fetchall() or []) if r.get("place_id")]
                    out["work_places"] = len(place_ids)

                if self._table_exists(conn, self.t_alias_candidates):
                    with conn.cursor() as cur:
                        cur.execute(
                            f"""
                            SELECT COUNT(*)::int AS n
                            FROM {_q_ident(self.t_alias_candidates)} ac
                            JOIN {_q_ident(self.t_place_candidates)} pc
                              ON pc.place_candidate_id = ac.place_candidate_id
                            WHERE pc.place_set_id::text = %s
                            """,
                            (psid,),
                        )
                        out["work_aliases"] = int((cur.fetchone() or {}).get("n") or 0)

                if self._table_exists(conn, "geo_work.node_place_map_work"):
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT COUNT(*)::int AS n
                            FROM geo_work.node_place_map_work
                            WHERE place_set_id::text = %s
                            """,
                            (psid,),
                        )
                        out["work_node_maps"] = int((cur.fetchone() or {}).get("n") or 0)

                if self._table_exists(conn, self.t_name_candidates):
                    with conn.cursor() as cur:
                        cur.execute(
                            f"""
                            SELECT COUNT(*)::int AS n
                            FROM {_q_ident(self.t_name_candidates)}
                            WHERE place_set_id::text = %s
                            """,
                            (psid,),
                        )
                        out["name_candidates"] = int((cur.fetchone() or {}).get("n") or 0)

                if self._table_exists(conn, self.t_name_feedback):
                    with conn.cursor() as cur:
                        cur.execute(
                            f"""
                            SELECT COUNT(*)::int AS n
                            FROM {_q_ident(self.t_name_feedback)}
                            WHERE place_set_id::text = %s
                            """,
                            (psid,),
                        )
                        out["name_feedback"] = int((cur.fetchone() or {}).get("n") or 0)

                if self._table_exists(conn, self.t_poi_stop_feedback):
                    with conn.cursor() as cur:
                        cur.execute(
                            f"""
                            SELECT COUNT(*)::int AS n
                            FROM {_q_ident(self.t_poi_stop_feedback)}
                            WHERE place_set_id::text = %s
                            """,
                            (psid,),
                        )
                        out["type_feedback"] = int((cur.fetchone() or {}).get("n") or 0)

                has_selection_log = self._table_exists(conn, "geo_work.selection_log")
                if has_selection_log:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT COUNT(*)::int AS n
                            FROM geo_work.selection_log
                            WHERE chosen_set_id::text = %s
                            """,
                            (psid,),
                        )
                        out["selection_logs_as_chosen"] = int((cur.fetchone() or {}).get("n") or 0)
                        cur.execute(
                            """
                            SELECT COUNT(*)::int AS n
                            FROM geo_work.selection_log
                            WHERE %s::uuid = ANY(COALESCE(rejected_set_ids, ARRAY[]::uuid[]))
                            """,
                            (psid,),
                        )
                        out["selection_logs_with_rejected_ref"] = int((cur.fetchone() or {}).get("n") or 0)

                if place_ids and self._table_exists(conn, self.t_prod_places):
                    arr = self._uuid_array_literal(place_ids)
                    with conn.cursor() as cur:
                        cur.execute(
                            f"""
                            SELECT COUNT(*)::int AS n
                            FROM {_q_ident(self.t_prod_places)}
                            WHERE place_id = ANY(%s::uuid[])
                            """,
                            (arr,),
                        )
                        out["prod_places"] = int((cur.fetchone() or {}).get("n") or 0)

                if place_ids and self._table_exists(conn, self.t_prod_aliases):
                    arr = self._uuid_array_literal(place_ids)
                    with conn.cursor() as cur:
                        cur.execute(
                            f"""
                            SELECT COUNT(*)::int AS n
                            FROM {_q_ident(self.t_prod_aliases)}
                            WHERE place_id = ANY(%s::uuid[])
                            """,
                            (arr,),
                        )
                        out["prod_aliases"] = int((cur.fetchone() or {}).get("n") or 0)

                if place_ids and self._table_exists(conn, self.t_prod_node_map):
                    arr = self._uuid_array_literal(place_ids)
                    with conn.cursor() as cur:
                        cur.execute(
                            f"""
                            SELECT COUNT(*)::int AS n
                            FROM {_q_ident(self.t_prod_node_map)}
                            WHERE place_id = ANY(%s::uuid[])
                            """,
                            (arr,),
                        )
                        out["prod_node_maps"] = int((cur.fetchone() or {}).get("n") or 0)

                if dry_run:
                    return out

                if delete_selection_logs and has_selection_log:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE geo_work.selection_log
                            SET rejected_set_ids = array_remove(COALESCE(rejected_set_ids, ARRAY[]::uuid[]), %s::uuid)
                            WHERE %s::uuid = ANY(COALESCE(rejected_set_ids, ARRAY[]::uuid[]))
                            """,
                            (psid, psid),
                        )
                        out["updated_selection_logs_arrays"] = int(cur.rowcount or 0)
                        cur.execute(
                            """
                            DELETE FROM geo_work.selection_log
                            WHERE chosen_set_id::text = %s
                            """,
                            (psid,),
                        )
                        out["deleted_selection_logs"] = int(cur.rowcount or 0)

                if delete_prod_entities and place_ids:
                    arr = self._uuid_array_literal(place_ids)
                    if self._table_exists(conn, self.t_prod_node_map):
                        with conn.cursor() as cur:
                            cur.execute(
                                f"""
                                DELETE FROM {_q_ident(self.t_prod_node_map)}
                                WHERE place_id = ANY(%s::uuid[])
                                """,
                                (arr,),
                            )
                            out["deleted_prod_node_maps"] = int(cur.rowcount or 0)
                    if self._table_exists(conn, self.t_prod_aliases):
                        with conn.cursor() as cur:
                            cur.execute(
                                f"""
                                DELETE FROM {_q_ident(self.t_prod_aliases)}
                                WHERE place_id = ANY(%s::uuid[])
                                """,
                                (arr,),
                            )
                            out["deleted_prod_aliases"] = int(cur.rowcount or 0)
                    if self._table_exists(conn, self.t_prod_places):
                        with conn.cursor() as cur:
                            cur.execute(
                                f"""
                                DELETE FROM {_q_ident(self.t_prod_places)}
                                WHERE place_id = ANY(%s::uuid[])
                                """,
                                (arr,),
                            )
                            out["deleted_prod_places"] = int(cur.rowcount or 0)

                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        DELETE FROM {_q_ident(self.t_place_sets)}
                        WHERE {set_id_col}::text = %s
                        """,
                        (psid,),
                    )
                    out["deleted_place_set"] = int(cur.rowcount or 0)

            if out["deleted_place_set"] == 0:
                raise RuntimeError(f"place_set_id not found: {psid}")
            return out

    def list_ranked_place_sets(self, *, context_key: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
            with _conn_ctx() as conn:
                sql = """
                SELECT
                ps.place_set_id::text,
                ps.rank_score,
                m.n_places,
                m.alias_conflict_rate,
                ps.created_at
                FROM geo_work.place_candidate_sets ps
                LEFT JOIN geo_work.place_set_metrics m
                ON m.place_set_id = ps.place_set_id
                WHERE (%s IS NULL OR ps.context_key = %s)
                ORDER BY ps.rank_score DESC NULLS LAST
                LIMIT %s
                """
                with conn.cursor() as cur:
                    cur.execute(sql, (context_key, context_key, int(limit)))
                    return list(cur.fetchall() or [])

    def list_pending_place_sets(self, *, context_key: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
            with _conn_ctx() as conn:
                if not self._table_exists(conn, "geo_work.selection_log"):
                    return self.list_candidate_sets(context_key=context_key, limit=limit)

                cols = self._table_columns(conn, self.t_place_sets)

                id_col = "place_set_id" if "place_set_id" in cols else "candidate_set_id"
                created_col = "created_at" if "created_at" in cols else ("extracted_at" if "extracted_at" in cols else None)
                rank_col = "rank_score" if "rank_score" in cols else ("score" if "score" in cols else None)
                ctx_col = "context_key" if "context_key" in cols else None

                created_expr = f"ps.{created_col}" if created_col else "NULL::timestamptz"
                rank_expr = f"ps.{rank_col}" if rank_col else "NULL::float8"
                ctx_expr = f"ps.{ctx_col}" if ctx_col else "NULL::text"

                sql = f"""
                SELECT
                ps.{id_col}::text AS place_set_id,
                {ctx_expr} AS context_key,
                {rank_expr} AS rank_score,
                {created_expr} AS created_at
                FROM {_q_ident(self.t_place_sets)} ps
                LEFT JOIN geo_work.selection_log sl
                ON sl.chosen_set_id = ps.{id_col}
                WHERE sl.selection_id IS NULL
                AND (%s IS NULL OR {ctx_expr} = %s)
                ORDER BY created_at DESC NULLS LAST
                LIMIT %s
                """
                with conn.cursor() as cur:
                    cur.execute(sql, (context_key, context_key, int(limit)))
                    return list(cur.fetchall() or [])


        # -----------------------------
        # Metrics
        # -----------------------------
    def get_place_set_metrics(self, place_set_id: str) -> Optional[Dict[str, Any]]:
            with _conn_ctx() as conn:
                if not self._table_exists(conn, self.t_metrics):
                    return None
                sql = "SELECT * FROM geo_work.place_set_metrics WHERE place_set_id::text=%s LIMIT 1"
                with conn.cursor() as cur:
                    cur.execute(sql, (place_set_id,))
                    row = cur.fetchone()
                    return dict(row) if row else None

        # -----------------------------
        # Promote / Approve (entire set)
        # -----------------------------
    def _count_work_places(self, conn, place_set_id: str) -> int:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*)::int AS n FROM geo_work.place_candidates WHERE place_set_id::text=%s", (place_set_id,))
                return int((cur.fetchone() or {}).get("n", 0))

    def _count_work_aliases(self, conn, place_set_id: str) -> int:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)::int AS n
                    FROM geo_work.alias_candidates ac
                    JOIN geo_work.place_candidates pc
                    ON pc.place_candidate_id = ac.place_candidate_id
                    WHERE pc.place_set_id::text = %s
                    """,
                    (place_set_id,),
                )
                return int((cur.fetchone() or {}).get("n", 0))

    def _count_work_nodes(self, conn, place_set_id: str) -> int:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*)::int AS n FROM geo_work.node_place_map_work WHERE place_set_id::text=%s", (place_set_id,))
                return int((cur.fetchone() or {}).get("n", 0))

    def _promote_places(self, conn, place_set_id: str) -> None:
            sql = """
            WITH latest_feedback AS (
              SELECT DISTINCT ON (f.place_candidate_id)
                f.place_candidate_id,
                f.chosen_name
              FROM geo_work.place_name_feedback f
              WHERE f.place_set_id::text = %s
              ORDER BY f.place_candidate_id, f.created_at DESC
            ), latest_type AS (
              SELECT DISTINCT ON (t.place_candidate_id)
                t.place_candidate_id,
                t.chosen_place_type
              FROM geo_work.poi_stop_feedback t
              WHERE t.place_set_id::text = %s
              ORDER BY t.place_candidate_id, t.created_at DESC
            )
            INSERT INTO geo_prod.places (place_id, canonical_name, place_type, geom, updated_at)
            SELECT
            pc.place_candidate_id,
            COALESCE(NULLIF(lf.chosen_name, ''), pc.proposed_canonical_name),
            COALESCE(lt.chosen_place_type, pc.model_place_type, pc.proposed_place_type),
            pc.center_geom,
            now()
            FROM geo_work.place_candidates pc
            LEFT JOIN latest_feedback lf
              ON lf.place_candidate_id = pc.place_candidate_id
            LEFT JOIN latest_type lt
              ON lt.place_candidate_id = pc.place_candidate_id
            WHERE pc.place_set_id::text = %s
            ON CONFLICT (place_id) DO UPDATE SET
            canonical_name = EXCLUDED.canonical_name,
            place_type     = EXCLUDED.place_type,
            geom           = EXCLUDED.geom,
            updated_at     = now();
            """
            with conn.cursor() as cur:
                cur.execute(sql, (place_set_id, place_set_id, place_set_id))

    def _promote_aliases(self, conn, place_set_id: str) -> None:
        sql_aliases = """
        INSERT INTO geo_prod.place_aliases
          (place_id, alias, normalized_alias, alias_kind, lang, updated_at)
        SELECT
          pc.place_candidate_id AS place_id,
          ac.alias,
          geo_work.normalize_alias(ac.alias) AS normalized_alias,
          ac.alias_kind,
          ac.lang,
          now()
        FROM geo_work.alias_candidates ac
        JOIN geo_work.place_candidates pc
          ON pc.place_candidate_id = ac.place_candidate_id
        WHERE pc.place_set_id::text = %s
        ON CONFLICT (place_id, normalized_alias) DO UPDATE SET
          alias      = EXCLUDED.alias,
          alias_kind = EXCLUDED.alias_kind,
          lang       = EXCLUDED.lang,
          updated_at = now();
        """

        sql_canon = """
        WITH latest_feedback AS (
          SELECT DISTINCT ON (f.place_candidate_id)
            f.place_candidate_id,
            f.chosen_name
          FROM geo_work.place_name_feedback f
          WHERE f.place_set_id::text = %s
          ORDER BY f.place_candidate_id, f.created_at DESC
        )
        INSERT INTO geo_prod.place_aliases
          (place_id, alias, normalized_alias, alias_kind, lang, updated_at)
        SELECT
          pc.place_candidate_id AS place_id,
          COALESCE(NULLIF(lf.chosen_name, ''), pc.proposed_canonical_name) AS alias,
          geo_work.normalize_alias(COALESCE(NULLIF(lf.chosen_name, ''), pc.proposed_canonical_name)) AS normalized_alias,
          'official' AS alias_kind,
          NULL::text AS lang,
          now()
        FROM geo_work.place_candidates pc
        LEFT JOIN latest_feedback lf
          ON lf.place_candidate_id = pc.place_candidate_id
        WHERE pc.place_set_id::text = %s
        ON CONFLICT (place_id, normalized_alias) DO UPDATE SET
          alias      = EXCLUDED.alias,
          alias_kind = EXCLUDED.alias_kind,
          lang       = EXCLUDED.lang,
          updated_at = now();
        """

        with conn.cursor() as cur:
            cur.execute(sql_aliases, (place_set_id,))
            cur.execute(sql_canon, (place_set_id, place_set_id))

    def _promote_node_map(self, conn, place_set_id: str) -> None:
        sql = """
        INSERT INTO geo_prod.node_place_map
          (node_id, place_id, confidence, mapping_source, updated_at)
        SELECT
          w.node_id,
          w.place_candidate_id AS place_id,
          w.confidence,
          CASE w.mapping_source
            WHEN 'auto' THEN 'phase2_auto'
            WHEN 'manual' THEN 'user_selected'
            ELSE 'phase2_auto'
          END AS mapping_source,
          now()
        FROM geo_work.node_place_map_work w
        WHERE w.place_set_id::text = %s
        ON CONFLICT (node_id) DO UPDATE SET
          place_id       = EXCLUDED.place_id,
          confidence     = EXCLUDED.confidence,
          mapping_source = EXCLUDED.mapping_source,
          updated_at     = now();
        """
        with conn.cursor() as cur:
            cur.execute(sql, (place_set_id,))

    def _get_set_params(self, conn, place_set_id: str) -> tuple[dict, Optional[str]]:
        sql = """
        SELECT params_used, context_key
        FROM geo_work.place_candidate_sets
        WHERE place_set_id::text = %s
        LIMIT 1
        """
        with conn.cursor() as cur:
            cur.execute(sql, (place_set_id,))
            row = cur.fetchone() or {}
        return (row.get("params_used") or {}), row.get("context_key")

    def _get_metrics_row(self, conn, place_set_id: str) -> dict:
        if not self._table_exists(conn, "geo_work.place_set_metrics"):
            return {}
        sql = "SELECT * FROM geo_work.place_set_metrics WHERE place_set_id::text=%s LIMIT 1"
        with conn.cursor() as cur:
            cur.execute(sql, (place_set_id,))
            row = cur.fetchone() or {}
        return dict(row)

    def _uuid_array_literal(self, ids: List[str]) -> str:
        return "{" + ",".join([str(x) for x in ids]) + "}" if ids else "{}"

    def _bulk_update_node_prod_names(self, conn, rename_rows: List[Dict[str, Any]]) -> int:
        """Apply a batch of (node_id, new_name) renames via the universal treater.

        Each row is routed through ``treat_stop(operation='name_repair')``
        so the name passes the canonical cascade, the place mapping is
        kept consistent, and an audit row is written. Idempotent — rows
        whose treated name matches the existing name are skipped (the
        treater's UPDATE is gated by `IS DISTINCT FROM`).
        """
        from phase3_routes.services.stop_quality import (
            StopTreatmentInput,
            treat_stop,
        )

        rows = [
            (str(r.get("node_id") or "").strip(), str(r.get("new_name") or "").strip())
            for r in (rename_rows or [])
            if str(r.get("node_id") or "").strip() and str(r.get("new_name") or "").strip()
        ]
        if not rows:
            return 0

        updated = 0
        for nid, new_name in rows:
            res = treat_stop(
                StopTreatmentInput(
                    operation="name_repair",
                    caller="phase2_client.bulk_rename",
                    node_id=nid,
                    proposed_name=new_name,
                ),
                conn,
            )
            if res.success:
                updated += 1
        return int(updated)

    def _bulk_delete_node_prod_nodes(
        self,
        conn,
        *,
        node_ids: List[str],
        purge_phase2_mappings: bool = True,
        mark_routes_dirty: bool = True,
        clear_route_approvals: bool = False,
        clear_route_prod_rows: bool = False,
    ) -> Dict[str, Any]:
        ids = sorted({str(x or "").strip() for x in (node_ids or []) if str(x or "").strip()})
        out: Dict[str, Any] = {
            "requested_node_count": len(ids),
            "impacted_route_ids": [],
            "deleted_node": 0,
            "deleted_prod_mappings": 0,
            "deleted_work_mappings": 0,
            "updated_seq_candidates": 0,
            "updated_route_prod_arrays": 0,
            "nulled_prior_matches": 0,
            "deleted_route_approvals": 0,
            "deleted_route_prod_rows": 0,
            "marked_routes_dirty": 0,
        }
        if not ids:
            return out

        impacted_routes: set[str] = set()
        with conn.cursor() as cur:
            if self._table_exists(conn, "route_work.stop_sequence_candidates") and self._table_exists(conn, "route_work.stop_sequence_candidate_sets"):
                cur.execute(
                    """
                    SELECT DISTINCT scs.route_id::text AS route_id
                    FROM route_work.stop_sequence_candidates ssc
                    JOIN route_work.stop_sequence_candidate_sets scs
                      ON scs.set_id = ssc.set_id
                    WHERE COALESCE(ssc.stop_node_ids, ARRAY[]::uuid[]) && %s::uuid[]
                    """,
                    (ids,),
                )
                impacted_routes.update(str(r.get("route_id")) for r in (cur.fetchall() or []) if r.get("route_id"))

            if self._table_exists(conn, "route_prod.routes"):
                cur.execute(
                    """
                    SELECT DISTINCT route_id::text AS route_id
                    FROM route_prod.routes
                    WHERE COALESCE(stop_node_ids, ARRAY[]::uuid[]) && %s::uuid[]
                    """,
                    (ids,),
                )
                impacted_routes.update(str(r.get("route_id")) for r in (cur.fetchall() or []) if r.get("route_id"))

            if self._table_exists(conn, "route_work.relation_stop_prior"):
                cur.execute(
                    """
                    SELECT DISTINCT route_id::text AS route_id
                    FROM route_work.relation_stop_prior
                    WHERE matched_stop_node_id = ANY(%s::uuid[])
                    """,
                    (ids,),
                )
                impacted_routes.update(str(r.get("route_id")) for r in (cur.fetchall() or []) if r.get("route_id"))

            impacted_route_ids = sorted(impacted_routes)
            out["impacted_route_ids"] = impacted_route_ids

            if purge_phase2_mappings:
                if self._table_exists(conn, self.t_prod_node_map):
                    cur.execute(
                        f"DELETE FROM {_q_ident(self.t_prod_node_map)} WHERE node_id = ANY(%s::uuid[])",
                        (ids,),
                    )
                    out["deleted_prod_mappings"] = int(cur.rowcount or 0)
                if self._table_exists(conn, "geo_work.node_place_map_work"):
                    cur.execute(
                        "DELETE FROM geo_work.node_place_map_work WHERE node_id = ANY(%s::uuid[])",
                        (ids,),
                    )
                    out["deleted_work_mappings"] = int(cur.rowcount or 0)

            if self._table_exists(conn, "route_work.stop_sequence_candidates"):
                cur.execute(
                    """
                    UPDATE route_work.stop_sequence_candidates AS s
                    SET stop_node_ids = COALESCE(
                        ARRAY(
                            SELECT x
                            FROM unnest(COALESCE(s.stop_node_ids, ARRAY[]::uuid[])) AS x
                            WHERE NOT (x = ANY(%s::uuid[]))
                        ),
                        ARRAY[]::uuid[]
                    )
                    WHERE COALESCE(s.stop_node_ids, ARRAY[]::uuid[]) && %s::uuid[]
                    """,
                    (ids, ids),
                )
                out["updated_seq_candidates"] = int(cur.rowcount or 0)

            if self._table_exists(conn, "route_prod.routes") and not clear_route_prod_rows:
                _touched_route_codes: set[str] = set()
                for _sid in ids:
                    _prune_result = prune_stop_node_id_from_routes(
                        conn=conn,
                        stop_node_id=_sid,
                        reason="phase2_client.delete_stops_bulk: cascading stop deletion",
                        pipeline_version=_PIPELINE_VERSION_PRUNE_BULK,
                        source_component=_SOURCE_COMPONENT_PHASE2,
                    )
                    _touched_route_codes.update(_prune_result.affected_route_codes or [])
                out["updated_route_prod_arrays"] = len(_touched_route_codes)

            if self._table_exists(conn, "route_work.relation_stop_prior"):
                cur.execute(
                    """
                    UPDATE route_work.relation_stop_prior
                    SET matched_stop_node_id = NULL,
                        match_dist_m = NULL
                    WHERE matched_stop_node_id = ANY(%s::uuid[])
                    """,
                    (ids,),
                )
                out["nulled_prior_matches"] = int(cur.rowcount or 0)

            if impacted_route_ids and clear_route_approvals and self._table_exists(conn, "route_work.route_approvals"):
                cur.execute(
                    """
                    DELETE FROM route_work.route_approvals
                    WHERE route_id = ANY(%s::uuid[])
                    """,
                    (impacted_route_ids,),
                )
                out["deleted_route_approvals"] = int(cur.rowcount or 0)

            if impacted_route_ids and clear_route_prod_rows and self._table_exists(conn, "route_prod.routes"):
                _bulk_del = delete_routes_prod_bulk(
                    conn=conn,
                    route_ids=list(impacted_route_ids),
                    reason="phase2_client.delete_stops_bulk: clear_route_prod_rows for impacted routes",
                    pipeline_version=_PIPELINE_VERSION_ROUTES_BULK,
                    source_component=_SOURCE_COMPONENT_PHASE2,
                )
                out["deleted_route_prod_rows"] = int(
                    _bulk_del.rows_affected.get("route_prod.routes", 0) or 0
                )

            if impacted_route_ids and mark_routes_dirty and self._table_exists(conn, "route_raw.route_jobs"):
                cur.execute(
                    """
                    UPDATE route_raw.route_jobs
                    SET status = 'needs_rebuild'
                    WHERE route_id = ANY(%s::uuid[])
                    """,
                    (impacted_route_ids,),
                )
                out["marked_routes_dirty"] = int(cur.rowcount or 0)

            if not self._table_exists(conn, "node_prod.nodes"):
                raise RuntimeError("Missing table node_prod.nodes")
            cur.execute(
                "DELETE FROM node_prod.nodes WHERE node_id = ANY(%s::uuid[])",
                (ids,),
            )
            out["deleted_node"] = int(cur.rowcount or 0)

        return out

    def _write_selection_log(
        self,
        conn,
        *,
        place_set_id: str,
        rejected_set_ids: List[str],
        params: Dict[str, Any],
        metrics: Dict[str, Any],
        context: Dict[str, Any],
    ) -> None:
        if not self._table_exists(conn, "geo_work.selection_log"):
            return
        sql = """
        INSERT INTO geo_work.selection_log
          (phase, object_type, chosen_set_id, rejected_set_ids, params, metrics, context)
        VALUES
          (2, 'places', %s, %s::uuid[], %s::jsonb, %s::jsonb, %s::jsonb)
        """
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    place_set_id,
                    self._uuid_array_literal(rejected_set_ids),
                    psycopg2.extras.Json(params),
                    psycopg2.extras.Json(metrics),
                    psycopg2.extras.Json(context),
                ),
            )

    def approve_place_set(
        self,
        place_set_id: str,
        *,
        rejected_set_ids: Optional[List[str]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> ApproveSummary:
        rejected_set_ids = rejected_set_ids or []
        context = context or {}

        with _conn_ctx() as conn:
            params_used, context_key = self._get_set_params(conn, place_set_id)
            metrics_row = self._get_metrics_row(conn, place_set_id)

            n_places = self._count_work_places(conn, place_set_id)
            n_aliases = self._count_work_aliases(conn, place_set_id)
            n_nodes = self._count_work_nodes(conn, place_set_id)

            self._promote_places(conn, place_set_id)
            self._promote_aliases(conn, place_set_id)
            self._promote_node_map(conn, place_set_id)

            self._write_selection_log(
                conn,
                place_set_id=place_set_id,
                rejected_set_ids=rejected_set_ids,
                params=params_used,
                metrics=metrics_row,
                context={**context, "context_key": context_key},
            )

            return ApproveSummary(
                place_set_id=str(place_set_id),
                n_places_written=int(n_places),
                n_aliases_written=int(n_aliases) + int(n_places),  # + canonical aliases
                n_nodes_mapped=int(n_nodes),
                reason="promote_entire_set",
            )

    def prod_counts(self) -> Dict[str, int]:
        out = {"n_places": 0, "n_aliases": 0, "n_mappings": 0}
        with _conn_ctx() as conn:
            out["n_places"] = self._count_table(conn, self.t_prod_places)
            out["n_aliases"] = self._count_table(conn, self.t_prod_aliases)
            out["n_mappings"] = self._count_table(conn, self.t_prod_node_map)
        return out

    # -----------------------------
    # Widget-friendly: views you created
    # -----------------------------
    def get_place_set_points(self, place_set_id: str, *, limit: int = 20000) -> List[Dict[str, Any]]:
        sql = """
        SELECT
          place_set_id::text,
          place_candidate_id::text,
          proposed_canonical_name,
          proposed_place_type,
          node_id::text,
          confidence,
          mapping_source,
          node_type,
          lat, lon
        FROM geo_work.v_place_set_points
        WHERE place_set_id::text = %s
        LIMIT %s
        """
        with _conn_ctx() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (place_set_id, int(limit)))
                return list(cur.fetchall() or [])

    def get_place_candidate_points(
        self,
        *,
        place_set_id: str,
        place_candidate_id: str,
        limit: int = 5000,
    ) -> List[Dict[str, Any]]:
        sql = """
        SELECT
          place_set_id::text,
          place_candidate_id::text,
          proposed_canonical_name,
          proposed_place_type,
          node_id::text,
          confidence,
          mapping_source,
          node_type,
          lat, lon
        FROM geo_work.v_place_set_points
        WHERE place_set_id::text = %s
          AND place_candidate_id::text = %s
        ORDER BY confidence DESC NULLS LAST
        LIMIT %s
        """
        with _conn_ctx() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (place_set_id, place_candidate_id, int(limit)))
                return list(cur.fetchall() or [])

    def get_place_candidate_points_multi(
        self,
        *,
        place_set_id: str,
        place_candidate_ids: List[str],
        limit: int = 12000,
    ) -> List[Dict[str, Any]]:
        ids = [str(x) for x in (place_candidate_ids or []) if str(x).strip()]
        if not ids:
            return []
        arr = self._uuid_array_literal(ids)
        sql = """
        SELECT
          place_set_id::text,
          place_candidate_id::text,
          proposed_canonical_name,
          proposed_place_type,
          node_id::text,
          confidence,
          mapping_source,
          node_type,
          lat, lon
        FROM geo_work.v_place_set_points
        WHERE place_set_id::text = %s
          AND place_candidate_id = ANY(%s::uuid[])
        ORDER BY place_candidate_id, confidence DESC NULLS LAST
        LIMIT %s
        """
        with _conn_ctx() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (place_set_id, arr, int(limit)))
                return list(cur.fetchall() or [])

    def list_alias_collision_rows(self, place_set_id: str, *, limit: int = 20000) -> List[Dict[str, Any]]:
        """
        Rows for duplicate-name diagnostics in widgets.
        Includes alias text + normalized alias + place candidate metadata and centroid stats.
        """
        sql = """
        WITH place_points AS (
          SELECT
            v.place_candidate_id::text AS place_candidate_id,
            COUNT(*)::int AS n_nodes,
            AVG(v.lat)::float8 AS center_lat,
            AVG(v.lon)::float8 AS center_lon
          FROM geo_work.v_place_set_points v
          WHERE v.place_set_id::text = %s
          GROUP BY v.place_candidate_id
        )
        SELECT
          ac.alias,
          geo_work.normalize_alias(ac.alias) AS alias_norm,
          ac.place_candidate_id::text AS place_candidate_id,
          pc.proposed_canonical_name,
          pc.proposed_place_type,
          COALESCE(pp.n_nodes, 0)::int AS n_nodes,
          pp.center_lat,
          pp.center_lon
        FROM geo_work.alias_candidates ac
        JOIN geo_work.place_candidates pc
          ON pc.place_candidate_id = ac.place_candidate_id
        LEFT JOIN place_points pp
          ON pp.place_candidate_id = ac.place_candidate_id::text
        WHERE pc.place_set_id::text = %s
        ORDER BY alias_norm, pc.proposed_canonical_name, ac.alias
        LIMIT %s
        """
        with _conn_ctx() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (place_set_id, place_set_id, int(limit)))
                return list(cur.fetchall() or [])

    def update_node_prod_location(self, node_id: str, *, lat: float, lon: float) -> Dict[str, Any]:
        """Move a node to (lat, lon) via the universal treater.

        Routes through ``treat_stop(operation='snap_align')`` so the
        relocation also re-validates the name (forbidden names get
        repaired via the contextual cascade) and refreshes the
        ``geo_prod.places`` mapping for the new location.
        """
        from phase3_routes.services.stop_quality import (
            StopTreatmentInput,
            treat_stop,
        )

        nid = str(node_id or "").strip()
        if not nid:
            raise RuntimeError("node_id is required")
        with _conn_ctx() as conn:
            if not self._table_exists(conn, "node_prod.nodes"):
                raise RuntimeError("Missing table node_prod.nodes")
            cols = self._table_columns(conn, "node_prod.nodes")
            if "geom" not in cols:
                # Legacy lat/lon-column schema — keep direct UPDATE.
                # No production database is on this path; left for
                # backward-compat with old test fixtures.
                with conn.cursor() as cur:
                    set_parts: List[str] = []
                    params: List[Any] = []
                    if "lat" in cols:
                        set_parts.append("lat = %s")
                        params.append(float(lat))
                    if "lon" in cols:
                        set_parts.append("lon = %s")
                        params.append(float(lon))
                    if "updated_at" in cols:
                        set_parts.append("updated_at = now()")
                    if not set_parts:
                        raise RuntimeError("node_prod.nodes has neither geom nor lat/lon columns")
                    params.append(nid)
                    cur.execute(
                        f"""
                        UPDATE node_prod.nodes
                        SET {", ".join(set_parts)}
                        WHERE node_id::text = %s
                        RETURNING node_id::text
                        """,
                        tuple(params),
                    )
                    if cur.fetchone() is None:
                        raise RuntimeError(f"node_id not found: {nid}")
                return self.get_node_prod_detail(nid)

            # Modern path: snap_align via treater.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT name FROM node_prod.nodes WHERE node_id::text = %s",
                    (nid,),
                )
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError(f"node_id not found: {nid}")
                current_name = row.get("name") if isinstance(row, dict) else row[0]

            res = treat_stop(
                StopTreatmentInput(
                    operation="snap_align",
                    caller="phase2_client.update_location",
                    node_id=nid,
                    proposed_name=current_name or "",
                    proposed_lat=float(lat),
                    proposed_lon=float(lon),
                ),
                conn,
            )
            if not res.success:
                raise RuntimeError(f"snap_align failed for {nid}: {res.error}")
            conn.commit()
        return self.get_node_prod_detail(nid)

    def delete_node_prod_node(
        self,
        node_id: str,
        *,
        purge_phase2_mappings: bool = True,
        mark_routes_dirty: bool = True,
        clear_route_approvals: bool = True,
        clear_route_prod_rows: bool = True,
        recompute_sequences_and_valhalla: bool = False,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        nid = str(node_id or "").strip()
        if not nid:
            raise RuntimeError("node_id is required")
        out: Dict[str, Any] = {
            "node_id": nid,
            "dry_run": bool(dry_run),
            "impacted_route_ids": [],
            "deleted_node": 0,
            "deleted_prod_mappings": 0,
            "deleted_work_mappings": 0,
            "updated_seq_candidates": 0,
            "updated_route_prod_arrays": 0,
            "nulled_prior_matches": 0,
            "deleted_route_approvals": 0,
            "deleted_route_prod_rows": 0,
            "marked_routes_dirty": 0,
            "recompute": [],
        }
        with _conn_ctx() as conn:
            with conn.cursor() as cur:
                impacted_routes: List[str] = []
                # A) routes impacted through work sequence candidates
                if self._table_exists(conn, "route_work.stop_sequence_candidates") and self._table_exists(conn, "route_work.stop_sequence_candidate_sets"):
                    cur.execute(
                        """
                        SELECT DISTINCT scs.route_id::text AS route_id
                        FROM route_work.stop_sequence_candidates ssc
                        JOIN route_work.stop_sequence_candidate_sets scs
                          ON scs.set_id = ssc.set_id
                        WHERE %s::uuid = ANY(COALESCE(ssc.stop_node_ids, ARRAY[]::uuid[]))
                        """,
                        (nid,),
                    )
                    impacted_routes.extend([str(r.get("route_id")) for r in (cur.fetchall() or []) if r.get("route_id")])

                # B) routes impacted in route_prod
                if self._table_exists(conn, "route_prod.routes"):
                    cur.execute(
                        """
                        SELECT DISTINCT route_id::text AS route_id
                        FROM route_prod.routes
                        WHERE %s::uuid = ANY(COALESCE(stop_node_ids, ARRAY[]::uuid[]))
                        """,
                        (nid,),
                    )
                    impacted_routes.extend([str(r.get("route_id")) for r in (cur.fetchall() or []) if r.get("route_id")])

                # C) routes impacted only through matched stop prior
                if self._table_exists(conn, "route_work.relation_stop_prior"):
                    cur.execute(
                        """
                        SELECT DISTINCT route_id::text AS route_id
                        FROM route_work.relation_stop_prior
                        WHERE matched_stop_node_id::text = %s
                        """,
                        (nid,),
                    )
                    impacted_routes.extend([str(r.get("route_id")) for r in (cur.fetchall() or []) if r.get("route_id")])

                impacted_routes = sorted(set([x for x in impacted_routes if x]))
                out["impacted_route_ids"] = impacted_routes

                if dry_run:
                    cur.execute("SELECT 1 FROM node_prod.nodes WHERE node_id::text = %s LIMIT 1", (nid,))
                    out["node_exists"] = bool(cur.fetchone())
                    if self._table_exists(conn, "route_work.stop_sequence_candidates"):
                        cur.execute(
                            """
                            SELECT COUNT(*)::int AS n
                            FROM route_work.stop_sequence_candidates
                            WHERE %s::uuid = ANY(COALESCE(stop_node_ids, ARRAY[]::uuid[]))
                            """,
                            (nid,),
                        )
                        out["seq_candidates_with_node"] = int((cur.fetchone() or {}).get("n") or 0)
                    if self._table_exists(conn, "route_prod.routes"):
                        cur.execute(
                            """
                            SELECT COUNT(*)::int AS n
                            FROM route_prod.routes
                            WHERE %s::uuid = ANY(COALESCE(stop_node_ids, ARRAY[]::uuid[]))
                            """,
                            (nid,),
                        )
                        out["route_prod_rows_with_node"] = int((cur.fetchone() or {}).get("n") or 0)
                    if self._table_exists(conn, "route_work.relation_stop_prior"):
                        cur.execute(
                            """
                            SELECT COUNT(*)::int AS n
                            FROM route_work.relation_stop_prior
                            WHERE matched_stop_node_id::text = %s
                            """,
                            (nid,),
                        )
                        out["prior_matches_with_node"] = int((cur.fetchone() or {}).get("n") or 0)
                    return out

                if purge_phase2_mappings:
                    if self._table_exists(conn, self.t_prod_node_map):
                        cur.execute(
                            f"DELETE FROM {_q_ident(self.t_prod_node_map)} WHERE node_id::text = %s",
                            (nid,),
                        )
                        out["deleted_prod_mappings"] = int(cur.rowcount or 0)
                    if self._table_exists(conn, "geo_work.node_place_map_work"):
                        cur.execute(
                            "DELETE FROM geo_work.node_place_map_work WHERE node_id::text = %s",
                            (nid,),
                        )
                        out["deleted_work_mappings"] = int(cur.rowcount or 0)

                if self._table_exists(conn, "route_work.stop_sequence_candidates"):
                    cur.execute(
                        """
                        UPDATE route_work.stop_sequence_candidates
                        SET stop_node_ids = array_remove(COALESCE(stop_node_ids, ARRAY[]::uuid[]), %s::uuid)
                        WHERE %s::uuid = ANY(COALESCE(stop_node_ids, ARRAY[]::uuid[]))
                        """,
                        (nid, nid),
                    )
                    out["updated_seq_candidates"] = int(cur.rowcount or 0)

                if self._table_exists(conn, "route_prod.routes"):
                    _prune_result = prune_stop_node_id_from_routes(
                        conn=conn,
                        stop_node_id=nid,
                        reason="phase2_client.delete_stop_single: cascading stop deletion",
                        pipeline_version=_PIPELINE_VERSION_PRUNE_SINGLE,
                        source_component=_SOURCE_COMPONENT_PHASE2,
                    )
                    out["updated_route_prod_arrays"] = int(
                        _prune_result.rows_affected.get("route_prod.routes", 0) or 0
                    )

                if self._table_exists(conn, "route_work.relation_stop_prior"):
                    cur.execute(
                        """
                        UPDATE route_work.relation_stop_prior
                        SET matched_stop_node_id = NULL,
                            match_dist_m = NULL
                        WHERE matched_stop_node_id::text = %s
                        """,
                        (nid,),
                    )
                    out["nulled_prior_matches"] = int(cur.rowcount or 0)

                if impacted_routes and clear_route_approvals and self._table_exists(conn, "route_work.route_approvals"):
                    cur.execute(
                        """
                        DELETE FROM route_work.route_approvals
                        WHERE route_id = ANY(%s::uuid[])
                        """,
                        (self._uuid_array_literal(impacted_routes),),
                    )
                    out["deleted_route_approvals"] = int(cur.rowcount or 0)

                if impacted_routes and clear_route_prod_rows and self._table_exists(conn, "route_prod.routes"):
                    _bulk_del = delete_routes_prod_bulk(
                        conn=conn,
                        route_ids=list(impacted_routes),
                        reason="phase2_client.delete_stop_single: clear_route_prod_rows for impacted routes",
                        pipeline_version=_PIPELINE_VERSION_ROUTES_SINGLE,
                        source_component=_SOURCE_COMPONENT_PHASE2,
                    )
                    out["deleted_route_prod_rows"] = int(
                        _bulk_del.rows_affected.get("route_prod.routes", 0) or 0
                    )

                if impacted_routes and mark_routes_dirty and self._table_exists(conn, "route_raw.route_jobs"):
                    cur.execute(
                        """
                        UPDATE route_raw.route_jobs
                        SET status = 'needs_rebuild'
                        WHERE route_id = ANY(%s::uuid[])
                        """,
                        (self._uuid_array_literal(impacted_routes),),
                    )
                    out["marked_routes_dirty"] = int(cur.rowcount or 0)

                if not self._table_exists(conn, "node_prod.nodes"):
                    raise RuntimeError("Missing table node_prod.nodes")
                cur.execute(
                    "DELETE FROM node_prod.nodes WHERE node_id::text = %s",
                    (nid,),
                )
                out["deleted_node"] = int(cur.rowcount or 0)

        if out["deleted_node"] == 0:
            raise RuntimeError(f"node_id not found or already deleted: {nid}")

        if recompute_sequences_and_valhalla and out["impacted_route_ids"]:
            try:
                from datamind_console.phases.phase3_routes.client import Phase3Client

                p3 = Phase3Client()
                for rid in out["impacted_route_ids"]:
                    rr: Dict[str, Any] = {"route_id": rid, "step20_ok": False, "step30_ok": False}
                    try:
                        s20 = p3.run_step_20_sequences(route_id=uuid.UUID(str(rid)))
                        rr["step20_ok"] = True
                        rr["step20"] = s20
                    except Exception as e:
                        rr["error"] = f"step20: {e}"
                        out["recompute"].append(rr)
                        continue

                    set_id = str((rr.get("step20") or {}).get("stop_sequence_set_id") or "")
                    if not set_id:
                        rr["error"] = "step20 succeeded but no stop_sequence_set_id"
                        out["recompute"].append(rr)
                        continue

                    # pick best available sequence candidate (rank asc)
                    with _conn_ctx() as conn2:
                        with conn2.cursor() as cur2:
                            cur2.execute(
                                """
                                SELECT candidate_id::text
                                FROM route_work.stop_sequence_candidates
                                WHERE set_id::text = %s
                                ORDER BY rank ASC, created_at ASC
                                LIMIT 1
                                """,
                                (set_id,),
                            )
                            crow = cur2.fetchone() or {}
                    cand_id = str(crow.get("candidate_id") or "")
                    if not cand_id:
                        rr["error"] = "no sequence candidate available after step20"
                        out["recompute"].append(rr)
                        continue

                    try:
                        s30 = p3.run_step_30_geometry(
                            route_id=uuid.UUID(str(rid)),
                            stop_sequence_candidate_id=uuid.UUID(cand_id),
                        )
                        rr["step30_ok"] = True
                        rr["step30"] = s30
                    except Exception as e:
                        rr["error"] = f"step30: {e}"
                    out["recompute"].append(rr)
            except Exception as e:
                out["recompute_error"] = str(e)
        return out

    def normalize_prod_nodes_global(
        self,
        *,
        dedup_radius_m: float = 2.0,
        delete_bad_named_nodes: bool = False,
        normalize_names: bool = True,
        only_stop_nodes: bool = True,
        apply_delete_limit: Optional[int] = None,
        max_delete_apply: int = 5000,
        allow_large_delete_apply: bool = False,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """
        Global cleaner/normalizer for existing node_prod nodes.
        - Normalizes display names (title case, generic cleanup)
        - Optionally deletes bad/generic/numeric-only names
        - Deduplicates nearby nodes only when location is close and names are near-equivalent,
          or when a generic placeholder can collapse into a better nearby name
        - Applies set-based deletes so downstream cleanup stays consistent without per-node loops
        """
        started_at = perf_counter()
        radius = float(max(0.5, min(float(dedup_radius_m), 20.0)))
        selected_delete_limit: Optional[int] = None
        if apply_delete_limit is not None:
            try:
                limit_int = int(apply_delete_limit)
            except Exception:
                limit_int = 0
            if limit_int > 0:
                selected_delete_limit = limit_int

        guardrail_max = max(1, int(max_delete_apply))
        out: Dict[str, Any] = {
            "dry_run": bool(dry_run),
            "scope": "node_prod.nodes",
            "only_stop_nodes": bool(only_stop_nodes),
            "dedup_radius_m": radius,
            "apply_delete_limit": selected_delete_limit,
            "guardrail_max_delete_apply": guardrail_max,
            "allow_large_delete_apply": bool(allow_large_delete_apply),
            "apply_strategy": "bulk",
            "total_nodes_scanned": 0,
            "clusters_total": 0,
            "duplicate_clusters": 0,
            "rename_candidates": 0,
            "bad_name_candidates": 0,
            "duplicate_delete_candidates": 0,
            "final_delete_candidates": 0,
            "selected_delete_candidates": 0,
            "guardrail_blocked": False,
            "renamed_applied": 0,
            "deleted_applied": 0,
            "delete_failures": 0,
            "impacted_routes_count": 0,
            "impacted_route_ids_sample": [],
            "selected_delete_ids_sample": [],
            "sample_renames": [],
            "sample_deletes": [],
            "sample_clusters": [],
            "timings_ms": {},
        }

        scan_started_at = perf_counter()
        rows: List[Dict[str, Any]] = []
        with _conn_ctx() as conn:
            if not self._table_exists(conn, "node_prod.nodes"):
                raise RuntimeError("Missing table node_prod.nodes")

            cols = self._table_columns(conn, "node_prod.nodes")
            select_parts = ["node_id::text AS node_id"]
            if "geom" in cols:
                select_parts.append("ST_Y(geom)::float8 AS lat")
                select_parts.append("ST_X(geom)::float8 AS lon")
            else:
                if "lat" in cols:
                    select_parts.append("lat::float8 AS lat")
                else:
                    select_parts.append("NULL::float8 AS lat")
                if "lon" in cols:
                    select_parts.append("lon::float8 AS lon")
                else:
                    select_parts.append("NULL::float8 AS lon")

            for c in ("node_type", "name", "ref", "updated_at"):
                select_parts.append(c if c in cols else f"NULL AS {c}")

            where_parts: List[str] = []
            if only_stop_nodes and "node_type" in cols:
                where_parts.append("node_type = 'STOP'")
            if "geom" in cols:
                where_parts.append("geom IS NOT NULL")
            elif "lat" in cols and "lon" in cols:
                where_parts.append("lat IS NOT NULL AND lon IS NOT NULL")
            where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

            sql = f"""
            SELECT {", ".join(select_parts)}
            FROM node_prod.nodes
            {where_sql}
            """
            with conn.cursor() as cur:
                cur.execute(sql)
                rows_raw = list(cur.fetchall() or [])

            for r in rows_raw:
                row = dict(r or {})
                nid = str(row.get("node_id") or "").strip()
                if not nid:
                    continue
                try:
                    row["lat"] = float(row.get("lat"))
                    row["lon"] = float(row.get("lon"))
                except Exception:
                    continue
                rows.append(row)

        out["total_nodes_scanned"] = len(rows)
        scan_ms = int((perf_counter() - scan_started_at) * 1000.0)
        out["timings_ms"]["scan"] = scan_ms
        if not rows:
            out["timings_ms"]["total"] = int((perf_counter() - started_at) * 1000.0)
            return out

        cluster_started_at = perf_counter()
        groups = _p2_cluster_nodes_within_radius(rows, radius_m=radius)
        out["clusters_total"] = len(groups)
        out["timings_ms"]["cluster"] = int((perf_counter() - cluster_started_at) * 1000.0)

        plan_started_at = perf_counter()
        rename_rows: List[Dict[str, Any]] = []
        bad_name_ids: set[str] = set()
        rename_target_by_node: Dict[str, str] = {}
        group_keeper_by_node: Dict[str, str] = {}
        group_size_by_node: Dict[str, int] = {}
        dedup_delete_ids: set[str] = set()

        for idxs in groups:
            members = [rows[i] for i in idxs]
            cluster_size = len(members)
            if cluster_size > 1:
                out["duplicate_clusters"] = int(out.get("duplicate_clusters") or 0) + 1

            semantic_groups = _p2_semantic_duplicate_groups(members)
            primary_keeper = max((g["keeper"] for g in semantic_groups), key=_p2_node_keep_score)
            keeper_id = str(primary_keeper.get("node_id") or "").strip()
            include_cluster_sample = cluster_size > 1 and len(out["sample_clusters"]) < 40
            sample_cluster_nodes: List[Dict[str, Any]] = [] if include_cluster_sample else []

            keeper_ids: List[str] = []
            for semantic_group in semantic_groups:
                semantic_keeper = semantic_group["keeper"]
                semantic_keeper_id = str(semantic_keeper.get("node_id") or "").strip()
                if semantic_keeper_id:
                    keeper_ids.append(semantic_keeper_id)
                member_rows = list(semantic_group.get("members") or [])
                semantic_group_size = len(member_rows)

                for member_idx, m in enumerate(member_rows):
                    nid = str(m.get("node_id") or "").strip()
                    if not nid:
                        continue
                    group_keeper_by_node[nid] = semantic_keeper_id
                    group_size_by_node[nid] = semantic_group_size

                    is_bad_name = _p2_row_bad_name(m)
                    if is_bad_name:
                        bad_name_ids.add(nid)
                    if member_idx > 0:
                        dedup_delete_ids.add(nid)

                    old_name = str(m.get("name") or "").strip()
                    new_name = _p2_row_normalized_name(m)
                    if new_name:
                        rename_target_by_node[nid] = new_name
                    force_parada_replacement = bool(is_bad_name and str(new_name).strip().lower() == "parada")
                    if new_name and new_name != old_name and (normalize_names or force_parada_replacement):
                        rename_rows.append(
                            {
                                "node_id": nid,
                                "old_name": old_name,
                                "new_name": new_name,
                                "ref": str(m.get("ref") or ""),
                            }
                        )

                    if include_cluster_sample:
                        sample_cluster_nodes.append(
                            {
                                "node_id": nid,
                                "name": old_name,
                                "ref": str(m.get("ref") or ""),
                                "is_keeper": 1 if member_idx == 0 else 0,
                                "bad_name": 1 if is_bad_name else 0,
                                "semantic_group_size": semantic_group_size,
                                "semantic_keeper_id": semantic_keeper_id,
                            }
                        )

            if include_cluster_sample:
                out["sample_clusters"].append(
                    {
                        "keeper_id": keeper_id,
                        "keeper_ids": sorted([kid for kid in keeper_ids if kid]),
                        "size": cluster_size,
                        "nodes": sample_cluster_nodes,
                    }
                )

        bad_delete_ids: set[str] = set()
        if delete_bad_named_nodes:
            for nid in bad_name_ids:
                replacement = str(rename_target_by_node.get(nid) or "").strip().lower()
                if replacement == "parada":
                    continue
                keeper_id = str(group_keeper_by_node.get(nid) or "")
                gsize = int(group_size_by_node.get(nid) or 1)
                if gsize <= 1:
                    bad_delete_ids.add(nid)
                elif keeper_id and keeper_id != nid:
                    bad_delete_ids.add(nid)

        delete_candidate_rows: List[Dict[str, Any]] = []
        for nid in sorted(dedup_delete_ids | bad_delete_ids):
            reason = (
                "duplicate+bad_name"
                if (nid in dedup_delete_ids and nid in bad_delete_ids)
                else ("duplicate" if nid in dedup_delete_ids else "bad_name")
            )
            delete_candidate_rows.append(
                {
                    "node_id": nid,
                    "reason": reason,
                    "keeper_id": str(group_keeper_by_node.get(nid) or ""),
                    "cluster_size": int(group_size_by_node.get(nid) or 1),
                }
            )
        delete_candidate_rows.sort(key=_p2_delete_candidate_sort_key)

        final_delete_ids = [str(r.get("node_id") or "") for r in delete_candidate_rows if str(r.get("node_id") or "")]
        final_delete_set = set(final_delete_ids)
        rename_rows = [r for r in rename_rows if str(r.get("node_id") or "") not in final_delete_set]

        selected_delete_ids = list(final_delete_ids)
        if selected_delete_limit is not None:
            selected_delete_ids = selected_delete_ids[:selected_delete_limit]

        out["rename_candidates"] = len(rename_rows)
        out["bad_name_candidates"] = len(bad_name_ids)
        out["duplicate_delete_candidates"] = len(dedup_delete_ids)
        out["final_delete_candidates"] = len(final_delete_ids)
        out["selected_delete_candidates"] = len(selected_delete_ids)
        out["guardrail_blocked"] = len(selected_delete_ids) > guardrail_max
        out["sample_renames"] = rename_rows[:150]
        out["sample_deletes"] = delete_candidate_rows[:250]
        out["selected_delete_ids_sample"] = selected_delete_ids[:120]
        out["timings_ms"]["plan"] = int((perf_counter() - plan_started_at) * 1000.0)

        if dry_run:
            out["timings_ms"]["total"] = int((perf_counter() - started_at) * 1000.0)
            return out

        if out["guardrail_blocked"] and not allow_large_delete_apply:
            raise RuntimeError(
                "Global normalizer apply blocked: "
                f"selected delete count {len(selected_delete_ids)} exceeds guardrail {guardrail_max}. "
                "Use a smaller apply_delete_limit or explicitly allow a large delete apply."
            )

        rename_apply_started_at = perf_counter()
        renamed_applied = 0
        delete_summary: Dict[str, Any] = {}
        with _conn_ctx() as conn:
            renamed_applied = self._bulk_update_node_prod_names(conn, rename_rows)
            out["timings_ms"]["rename_apply"] = int((perf_counter() - rename_apply_started_at) * 1000.0)

            delete_apply_started_at = perf_counter()
            delete_summary = self._bulk_delete_node_prod_nodes(
                conn,
                node_ids=selected_delete_ids,
                purge_phase2_mappings=True,
                mark_routes_dirty=True,
                clear_route_approvals=False,
                clear_route_prod_rows=False,
            )
            out["timings_ms"]["delete_apply"] = int((perf_counter() - delete_apply_started_at) * 1000.0)

        out["renamed_applied"] = int(renamed_applied)
        deleted_applied = int(delete_summary.get("deleted_node") or 0)
        out["deleted_applied"] = deleted_applied
        out["delete_failures"] = max(0, len(selected_delete_ids) - deleted_applied)
        impacted_route_ids = sorted(
            {
                str(rid or "").strip()
                for rid in (delete_summary.get("impacted_route_ids") or [])
                if str(rid or "").strip()
            }
        )
        out["impacted_routes_count"] = len(impacted_route_ids)
        out["impacted_route_ids_sample"] = impacted_route_ids[:120]
        out["bulk_delete_summary"] = {
            "requested_node_count": int(delete_summary.get("requested_node_count") or 0),
            "deleted_prod_mappings": int(delete_summary.get("deleted_prod_mappings") or 0),
            "deleted_work_mappings": int(delete_summary.get("deleted_work_mappings") or 0),
            "updated_seq_candidates": int(delete_summary.get("updated_seq_candidates") or 0),
            "updated_route_prod_arrays": int(delete_summary.get("updated_route_prod_arrays") or 0),
            "nulled_prior_matches": int(delete_summary.get("nulled_prior_matches") or 0),
            "deleted_route_approvals": int(delete_summary.get("deleted_route_approvals") or 0),
            "deleted_route_prod_rows": int(delete_summary.get("deleted_route_prod_rows") or 0),
            "marked_routes_dirty": int(delete_summary.get("marked_routes_dirty") or 0),
        }
        if out["delete_failures"] > 0:
            out["delete_errors"] = [
                {
                    "error": "bulk_delete_deleted_count_mismatch",
                    "selected_delete_candidates": len(selected_delete_ids),
                    "deleted_applied": deleted_applied,
                }
            ]
        out["timings_ms"]["total"] = int((perf_counter() - started_at) * 1000.0)
        return out

    def get_prod_place_points(self, *, place_id: Optional[str] = None, limit: int = 20000) -> List[Dict[str, Any]]:
        sql = """
        SELECT
          place_id::text,
          canonical_name,
          place_type,
          node_id::text,
          confidence,
          mapping_source,
          node_type,
          lat, lon
        FROM geo_prod.v_place_points
        WHERE (%s IS NULL OR place_id::text = %s)
        LIMIT %s
        """
        with _conn_ctx() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (place_id, place_id, int(limit)))
                return list(cur.fetchall() or [])

    def get_prod_points_for_place_set(self, place_set_id: str, *, limit: int = 20000) -> List[Dict[str, Any]]:
        """
        Return geo_prod points linked to a geo_work place_set via shared node_id.
        Useful to compare what a selected work set already has in prod.
        """
        psid = str(place_set_id or "").strip()
        if not psid:
            return []
        sql = """
        WITH set_nodes AS (
          SELECT DISTINCT node_id
          FROM geo_work.v_place_set_points
          WHERE place_set_id::text = %s
        )
        SELECT
          p.place_id::text,
          p.canonical_name,
          p.place_type,
          p.node_id::text,
          p.confidence,
          p.mapping_source,
          p.node_type,
          p.lat, p.lon
        FROM geo_prod.v_place_points p
        JOIN set_nodes s
          ON s.node_id::text = p.node_id::text
        LIMIT %s
        """
        with _conn_ctx() as conn:
            if not self._table_exists(conn, "geo_work.v_place_set_points"):
                return []
            if not self._table_exists(conn, "geo_prod.v_place_points"):
                return []
            with conn.cursor() as cur:
                cur.execute(sql, (psid, int(limit)))
                return list(cur.fetchall() or [])

    def get_prod_linked_point_count(self, place_set_id: str) -> int:
        psid = str(place_set_id or "").strip()
        if not psid:
            return 0
        sql = """
        WITH set_nodes AS (
          SELECT DISTINCT node_id
          FROM geo_work.v_place_set_points
          WHERE place_set_id::text = %s
        )
        SELECT COUNT(*)::int AS n
        FROM geo_prod.v_place_points p
        JOIN set_nodes s
          ON s.node_id::text = p.node_id::text
        """
        with _conn_ctx() as conn:
            if not self._table_exists(conn, "geo_work.v_place_set_points"):
                return 0
            if not self._table_exists(conn, "geo_prod.v_place_points"):
                return 0
            with conn.cursor() as cur:
                cur.execute(sql, (psid,))
                row = cur.fetchone() or {}
                return int(row.get("n") if isinstance(row, dict) else 0)

    def get_node_prod_detail(self, node_id: str) -> Dict[str, Any]:
        with _conn_ctx() as conn:
            if not self._table_exists(conn, "node_prod.nodes"):
                return {}
            cols = self._table_columns(conn, "node_prod.nodes")

            select_parts = ["node_id::text AS node_id"]
            if "geom" in cols:
                select_parts.append("ST_Y(geom)::float8 AS lat")
                select_parts.append("ST_X(geom)::float8 AS lon")
            else:
                if "lat" in cols:
                    select_parts.append("lat::float8 AS lat")
                else:
                    select_parts.append("NULL::float8 AS lat")
                if "lon" in cols:
                    select_parts.append("lon::float8 AS lon")
                else:
                    select_parts.append("NULL::float8 AS lon")

            optional = [
                "node_type",
                "name",
                "ref",
                "operator",
                "tag_kind",
                "source",
                "source_node_set_id",
                "chosen_candidate_id",
                "chosen_tags",
                "confidence",
                "approved_at",
                "updated_at",
            ]
            for c in optional:
                if c in cols:
                    if c.endswith("_id"):
                        select_parts.append(f"{c}::text AS {c}")
                    else:
                        select_parts.append(c)
                else:
                    select_parts.append(f"NULL AS {c}")

            sql = f"""
            SELECT {", ".join(select_parts)}
            FROM node_prod.nodes
            WHERE node_id::text = %s
            LIMIT 1
            """
            with conn.cursor() as cur:
                cur.execute(sql, (node_id,))
                row = cur.fetchone()
        return dict(row) if row else {}

    def list_prod_place_summary(self, *, place_type: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        sql = """
        SELECT
          place_id::text,
          canonical_name,
          place_type,
          n_nodes,
          avg_confidence,
          min_confidence,
          max_confidence,
          bbox
        FROM geo_prod.v_place_summary
        WHERE (%s IS NULL OR place_type = %s)
        ORDER BY n_nodes DESC
        LIMIT %s
        """
        with _conn_ctx() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (place_type, place_type, int(limit)))
                rows = list(cur.fetchall() or [])
        for r in rows:
            r["bbox"] = self._bbox_from_box2d_text(r.get("bbox"))
        return rows

    def get_place_type_map(self, place_ids: List[str]) -> Dict[str, str]:
        if not place_ids:
            return {}
        with _conn_ctx() as conn:
            if not self._table_exists(conn, self.t_prod_places):
                return {}
            arr = self._uuid_array_literal([str(x) for x in place_ids])
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT place_id::text AS place_id, place_type
                    FROM {_q_ident(self.t_prod_places)}
                    WHERE place_id = ANY(%s::uuid[])
                    """,
                    (arr,),
                )
                rows = list(cur.fetchall() or [])
        return {str(r.get("place_id")): str(r.get("place_type") or "") for r in rows if r.get("place_id")}

    # -----------------------------
    # Existing plots you already had (kept)
    # -----------------------------
    def get_alias_confidence_distribution(self, place_set_id: str) -> List[Dict[str, Any]]:
        sql = """
        SELECT ac.score AS confidence
        FROM geo_work.alias_candidates ac
        JOIN geo_work.place_candidates pc
          ON pc.place_candidate_id = ac.place_candidate_id
        WHERE pc.place_set_id::text = %s
          AND ac.score IS NOT NULL
        """
        with _conn_ctx() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (place_set_id,))
                return list(cur.fetchall() or [])

    def get_node_mapping_confidence(self, place_set_id: str) -> List[Dict[str, Any]]:
        sql = """
        SELECT node_id::text, place_candidate_id::text, confidence
        FROM geo_work.node_place_map_work
        WHERE place_set_id::text = %s
        """
        with _conn_ctx() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (place_set_id,))
                return list(cur.fetchall() or [])

    def get_alias_conflicts(self, place_set_id: str) -> List[Dict[str, Any]]:
        sql = """
        SELECT
          ac.alias,
          COUNT(DISTINCT ac.place_candidate_id) AS n_places
        FROM geo_work.alias_candidates ac
        JOIN geo_work.place_candidates pc
          ON pc.place_candidate_id = ac.place_candidate_id
        WHERE pc.place_set_id::text = %s
        GROUP BY ac.alias
        HAVING COUNT(DISTINCT ac.place_candidate_id) > 1
        ORDER BY n_places DESC
        """
        with _conn_ctx() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (place_set_id,))
                return list(cur.fetchall() or [])

    def get_place_candidate_detail(self, place_candidate_id: str) -> Dict[str, Any]:
        sql = """
        SELECT
          pc.place_candidate_id::text,
          pc.proposed_canonical_name,
          pc.proposed_place_type,
          pc.score,
          COUNT(ac.alias_candidate_id) AS n_aliases,
          ST_Y(pc.center_geom)::float AS lat,
          ST_X(pc.center_geom)::float AS lon
        FROM geo_work.place_candidates pc
        LEFT JOIN geo_work.alias_candidates ac
          ON ac.place_candidate_id = pc.place_candidate_id
        WHERE pc.place_candidate_id::text = %s
        GROUP BY pc.place_candidate_id
        """
        with _conn_ctx() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (place_candidate_id,))
                row = cur.fetchone()
                return dict(row) if row else {}

    def get_place_set_review_progress(self, place_set_id: str) -> Dict[str, int]:
        with _conn_ctx() as conn:
            if not self._table_exists(conn, self.t_place_candidates):
                return {"total": 0, "name_reviewed": 0, "type_reviewed": 0, "ready_for_prod": 0, "remaining": 0}

            has_name_fb = self._table_exists(conn, self.t_name_feedback)
            has_type_fb = self._table_exists(conn, "geo_work.poi_stop_feedback")

            if has_name_fb and has_type_fb:
                sql = f"""
                WITH latest_name AS (
                  SELECT DISTINCT ON (f.place_candidate_id)
                    f.place_candidate_id
                  FROM {_q_ident(self.t_name_feedback)} f
                  WHERE f.place_set_id::text = %s
                  ORDER BY f.place_candidate_id, f.created_at DESC
                ), latest_type AS (
                  SELECT DISTINCT ON (t.place_candidate_id)
                    t.place_candidate_id
                  FROM geo_work.poi_stop_feedback t
                  WHERE t.place_set_id::text = %s
                  ORDER BY t.place_candidate_id, t.created_at DESC
                )
                SELECT
                  COUNT(*)::int AS total,
                  COUNT(*) FILTER (WHERE ln.place_candidate_id IS NOT NULL)::int AS name_reviewed,
                  COUNT(*) FILTER (WHERE lt.place_candidate_id IS NOT NULL)::int AS type_reviewed,
                  COUNT(*) FILTER (WHERE ln.place_candidate_id IS NOT NULL AND lt.place_candidate_id IS NOT NULL)::int AS ready_for_prod
                FROM {_q_ident(self.t_place_candidates)} pc
                LEFT JOIN latest_name ln
                  ON ln.place_candidate_id = pc.place_candidate_id
                LEFT JOIN latest_type lt
                  ON lt.place_candidate_id = pc.place_candidate_id
                WHERE pc.place_set_id::text = %s
                """
                with conn.cursor() as cur:
                    cur.execute(sql, (place_set_id, place_set_id, place_set_id))
                    row = cur.fetchone() or {}
            else:
                sql = f"""
                SELECT COUNT(*)::int AS total
                FROM {_q_ident(self.t_place_candidates)}
                WHERE place_set_id::text = %s
                """
                with conn.cursor() as cur:
                    cur.execute(sql, (place_set_id,))
                    base = cur.fetchone() or {}
                row = {
                    "total": int(base.get("total") if isinstance(base, dict) else (base[0] if base else 0)),
                    "name_reviewed": 0,
                    "type_reviewed": 0,
                    "ready_for_prod": 0,
                }

        total = int(row.get("total") if isinstance(row, dict) else 0)
        ready = int(row.get("ready_for_prod") if isinstance(row, dict) else 0)
        out = {
            "total": total,
            "name_reviewed": int(row.get("name_reviewed") if isinstance(row, dict) else 0),
            "type_reviewed": int(row.get("type_reviewed") if isinstance(row, dict) else 0),
            "ready_for_prod": ready,
            "remaining": max(total - ready, 0),
        }
        return out

    def list_place_candidates_for_set(self, place_set_id: str, *, limit: int = 500) -> List[Dict[str, Any]]:
        with _conn_ctx() as conn:
            has_name_fb = self._table_exists(conn, self.t_name_feedback)
            has_type_fb = self._table_exists(conn, "geo_work.poi_stop_feedback")
            with conn.cursor() as cur:
                if has_name_fb and has_type_fb:
                    sql = """
                    WITH latest_name AS (
                      SELECT DISTINCT ON (f.place_candidate_id)
                        f.place_candidate_id,
                        f.chosen_name
                      FROM geo_work.place_name_feedback f
                      WHERE f.place_set_id::text = %s
                      ORDER BY f.place_candidate_id, f.created_at DESC
                    ), latest_type AS (
                      SELECT DISTINCT ON (t.place_candidate_id)
                        t.place_candidate_id,
                        t.chosen_place_type
                      FROM geo_work.poi_stop_feedback t
                      WHERE t.place_set_id::text = %s
                      ORDER BY t.place_candidate_id, t.created_at DESC
                    )
                    SELECT
                      pc.place_candidate_id::text,
                      pc.proposed_canonical_name,
                      pc.proposed_place_type,
                      pc.model_place_type,
                      pc.model_place_type_score,
                      pc.score,
                      ln.chosen_name,
                      lt.chosen_place_type,
                      COALESCE(NULLIF(ln.chosen_name, ''), pc.proposed_canonical_name) AS display_canonical_name,
                      COALESCE(lt.chosen_place_type, pc.model_place_type, pc.proposed_place_type) AS display_place_type,
                      (ln.place_candidate_id IS NOT NULL) AS name_reviewed,
                      (lt.place_candidate_id IS NOT NULL) AS type_reviewed,
                      ((ln.place_candidate_id IS NOT NULL) AND (lt.place_candidate_id IS NOT NULL)) AS ready_for_prod
                    FROM geo_work.place_candidates pc
                    LEFT JOIN latest_name ln
                      ON ln.place_candidate_id = pc.place_candidate_id
                    LEFT JOIN latest_type lt
                      ON lt.place_candidate_id = pc.place_candidate_id
                    WHERE pc.place_set_id::text = %s
                    ORDER BY pc.score DESC NULLS LAST, pc.created_at ASC
                    LIMIT %s
                    """
                    cur.execute(sql, (place_set_id, place_set_id, place_set_id, int(limit)))
                else:
                    sql = """
                    SELECT
                      pc.place_candidate_id::text,
                      pc.proposed_canonical_name,
                      pc.proposed_place_type,
                      pc.model_place_type,
                      pc.model_place_type_score,
                      pc.score,
                      NULL::text AS chosen_name,
                      NULL::text AS chosen_place_type,
                      pc.proposed_canonical_name AS display_canonical_name,
                      COALESCE(pc.model_place_type, pc.proposed_place_type) AS display_place_type,
                      false AS name_reviewed,
                      false AS type_reviewed,
                      false AS ready_for_prod
                    FROM geo_work.place_candidates pc
                    WHERE pc.place_set_id::text = %s
                    ORDER BY pc.score DESC NULLS LAST, pc.created_at ASC
                    LIMIT %s
                    """
                    cur.execute(sql, (place_set_id, int(limit)))
                return list(cur.fetchall() or [])

    # -----------------------------
    # Bridge: approved Phase3 requests -> one Phase2 work set
    # -----------------------------
    def list_approved_phase3_request_nodes(
        self,
        *,
        source: Optional[str] = "phase3_route",
        route_id: Optional[str] = None,
        request_ids: Optional[List[str]] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        where_parts: List[str] = [
            "r.status = 'approved'",
            "r.approved_node_id IS NOT NULL",
        ]
        params: List[Any] = []
        if source:
            where_parts.append("r.source = %s")
            params.append(str(source))
        if route_id:
            where_parts.append("r.route_id::text = %s")
            params.append(str(route_id))

        req_ids = [str(x).strip() for x in (request_ids or []) if str(x or "").strip()]
        if req_ids:
            where_parts.append("r.request_id::text = ANY(%s)")
            params.append(req_ids)

        sql = f"""
        SELECT
          r.request_id::text AS request_id,
          r.source,
          r.route_id::text AS route_id,
          r.seq,
          r.created_at,
          r.reviewed_at,
          r.approved_node_id::text AS node_id,
          COALESCE(NULLIF(r.name, ''), NULLIF(n.name, ''), NULLIF(r.ref, ''), ('node_' || LEFT(r.approved_node_id::text, 8))) AS canonical_name,
          COALESCE(NULLIF(r.node_type, ''), NULLIF(n.node_type, ''), 'STOP') AS node_type,
          ST_Y(n.geom)::float8 AS lat,
          ST_X(n.geom)::float8 AS lon,
          n.ref AS node_ref,
          n.operator AS node_operator,
          n.chosen_tags AS node_tags,
          r.tags AS request_tags
        FROM node_work.node_review_requests r
        JOIN node_prod.nodes n
          ON n.node_id = r.approved_node_id
        WHERE {' AND '.join(where_parts)}
        ORDER BY r.reviewed_at DESC NULLS LAST, r.created_at DESC
        LIMIT %s
        """
        params.append(int(limit))
        with _conn_ctx() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                return list(cur.fetchall() or [])

    def sync_phase3_approved_into_place_set(
        self,
        *,
        source: Optional[str] = "phase3_route",
        context_key: str = "phase3_requests_live",
        route_id: Optional[str] = None,
        request_ids: Optional[List[str]] = None,
        set_label: Optional[str] = None,
        limit: int = 5000,
    ) -> Dict[str, Any]:
        """
        Rebuild a single Phase2 place_set from approved Phase3 requests.
        Safe for repeated runs; intended as a dynamic workspace bridge.
        """
        out: Dict[str, Any] = {
            "ok": True,
            "place_set_id": None,
            "created_new_set": False,
            "synced_nodes": 0,
            "inserted_places": 0,
            "inserted_aliases": 0,
            "inserted_mappings": 0,
            "source_extract_run_id": None,
            "context_key": str(context_key or ""),
            "route_id": (str(route_id) if route_id else None),
            "request_ids_count": len([str(x).strip() for x in (request_ids or []) if str(x or "").strip()]),
        }
        with _conn_ctx() as conn:
            if not self._table_exists(conn, "geo_raw.extract_runs"):
                raise RuntimeError("Missing table geo_raw.extract_runs")
            if not self._table_exists(conn, self.t_place_sets):
                raise RuntimeError(f"Missing table {self.t_place_sets}")
            if not self._table_exists(conn, self.t_place_candidates):
                raise RuntimeError(f"Missing table {self.t_place_candidates}")
            if not self._table_exists(conn, self.t_alias_candidates):
                raise RuntimeError(f"Missing table {self.t_alias_candidates}")
            if not self._table_exists(conn, "geo_work.node_place_map_work"):
                raise RuntimeError("Missing table geo_work.node_place_map_work")
            cols_ps = self._table_columns(conn, self.t_place_sets)
            set_name_col = "name" if "name" in cols_ps else ("label" if "label" in cols_ps else None)

            # 1) choose/create source_extract_run_id (required FK in place_candidate_sets)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT extract_run_id::text
                    FROM geo_raw.extract_runs
                    ORDER BY extracted_at DESC
                    LIMIT 1
                    """
                )
                row = cur.fetchone() or {}
                source_extract_run_id = str(row.get("extract_run_id") or "")
                if not source_extract_run_id:
                    cur.execute(
                        """
                        INSERT INTO geo_raw.extract_runs (context_key, status)
                        VALUES (%s, 'ok')
                        RETURNING extract_run_id::text
                        """,
                        ("phase3_bridge_sync",),
                    )
                    source_extract_run_id = str((cur.fetchone() or {}).get("extract_run_id") or "")
            if not source_extract_run_id:
                raise RuntimeError("Could not create source_extract_run_id")
            out["source_extract_run_id"] = source_extract_run_id

            # 2) choose/create one live place_set
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT place_set_id::text
                    FROM {_q_ident(self.t_place_sets)}
                    WHERE context_key = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (context_key,),
                )
                row = cur.fetchone() or {}
                place_set_id = str(row.get("place_set_id") or "")
                if not place_set_id:
                    place_set_id = str(uuid.uuid4())
                    cur.execute(
                        f"""
                        INSERT INTO {_q_ident(self.t_place_sets)}
                          (place_set_id, source_extract_run_id, context_key, params_used)
                        VALUES
                          (%s, %s, %s, %s::jsonb)
                        """,
                        (
                            place_set_id,
                            source_extract_run_id,
                            context_key,
                            psycopg2.extras.Json(
                                {
                                    "source": "node_work.node_review_requests",
                                    "status": "approved",
                                    "filter_source": source,
                                    "filter_route_id": route_id,
                                    "filter_request_ids_count": out["request_ids_count"],
                                }
                            ),
                        ),
                    )
                    out["created_new_set"] = True
                else:
                    # Keep latest extract run id fresh on each sync
                    cur.execute(
                        f"""
                        UPDATE {_q_ident(self.t_place_sets)}
                        SET source_extract_run_id = %s,
                            params_used = %s::jsonb
                        WHERE place_set_id::text = %s
                        """,
                        (
                            source_extract_run_id,
                            psycopg2.extras.Json(
                                {
                                    "source": "node_work.node_review_requests",
                                    "status": "approved",
                                    "filter_source": source,
                                    "filter_route_id": route_id,
                                    "filter_request_ids_count": out["request_ids_count"],
                                }
                            ),
                            place_set_id,
                        ),
                    )
                if set_name_col and set_label:
                    cur.execute(
                        f"""
                        UPDATE {_q_ident(self.t_place_sets)}
                        SET {set_name_col} = %s
                        WHERE place_set_id::text = %s
                        """,
                        (str(set_label), place_set_id),
                    )
            out["place_set_id"] = place_set_id

            # 3) pull approved request nodes
            req_rows = self.list_approved_phase3_request_nodes(
                source=source,
                route_id=(str(route_id) if route_id else None),
                request_ids=request_ids,
                limit=int(limit),
            )
            out["synced_nodes"] = len(req_rows)

            # 4) clean previous content for this live set
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM geo_work.node_place_map_work WHERE place_set_id::text = %s",
                    (place_set_id,),
                )
                cur.execute(
                    f"DELETE FROM {_q_ident(self.t_place_candidates)} WHERE place_set_id::text = %s",
                    (place_set_id,),
                )

            # 5) repopulate
            n_places = 0
            n_aliases = 0
            n_maps = 0
            with conn.cursor() as cur:
                for r in req_rows:
                    node_id = str(r.get("node_id") or "")
                    if not node_id:
                        continue
                    place_candidate_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{place_set_id}:{node_id}"))
                    node_type = str(r.get("node_type") or "STOP").upper()
                    if node_type not in {"STOP", "POI", "STATION", "TERMINAL", "OTHER"}:
                        node_type = "STOP"
                    cname = _p2_normalize_display_name(str(r.get("canonical_name") or f"node_{node_id[:8]}"))
                    lat = r.get("lat")
                    lon = r.get("lon")
                    if lat is None or lon is None:
                        continue
                    provenance = {
                        "source": "phase3_approved_request",
                        "request_id": r.get("request_id"),
                        "route_id": r.get("route_id"),
                        "seq": r.get("seq"),
                        "request_tags": r.get("request_tags") or {},
                    }
                    cur.execute(
                        f"""
                        INSERT INTO {_q_ident(self.t_place_candidates)}
                          (place_candidate_id, place_set_id, proposed_canonical_name, proposed_place_type, center_geom, provenance, score)
                        VALUES
                          (%s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s::jsonb, %s)
                        """,
                        (
                            place_candidate_id,
                            place_set_id,
                            cname,
                            node_type,
                            float(lon),
                            float(lat),
                            psycopg2.extras.Json(provenance),
                            1.0,
                        ),
                    )
                    n_places += 1

                    aliases: List[str] = []
                    if cname.strip():
                        aliases.append(cname.strip())
                    ref_v = str(r.get("node_ref") or "").strip()
                    if ref_v:
                        aliases.append(ref_v)
                    op_v = str(r.get("node_operator") or "").strip()
                    if op_v:
                        aliases.append(op_v)
                    seen_alias: set[str] = set()
                    for alias in aliases:
                        k = alias.lower().strip()
                        if not k or k in seen_alias:
                            continue
                        seen_alias.add(k)
                        cur.execute(
                            f"""
                            INSERT INTO {_q_ident(self.t_alias_candidates)}
                              (alias_candidate_id, place_candidate_id, alias, alias_kind, lang, score)
                            VALUES
                              (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (place_candidate_id, alias) DO NOTHING
                            """,
                            (
                                str(uuid.uuid4()),
                                place_candidate_id,
                                alias,
                                "official" if alias == cname else "alt",
                                None,
                                1.0,
                            ),
                        )
                        n_aliases += int(cur.rowcount or 0)

                    cur.execute(
                        """
                        INSERT INTO geo_work.node_place_map_work
                          (place_set_id, node_id, place_candidate_id, confidence, mapping_source)
                        VALUES
                          (%s, %s, %s, %s, %s)
                        ON CONFLICT (place_set_id, node_id) DO UPDATE SET
                          place_candidate_id = EXCLUDED.place_candidate_id,
                          confidence = EXCLUDED.confidence,
                          mapping_source = EXCLUDED.mapping_source
                        """,
                        (place_set_id, node_id, place_candidate_id, 1.0, "manual"),
                    )
                    n_maps += 1

            out["inserted_places"] = n_places
            out["inserted_aliases"] = n_aliases
            out["inserted_mappings"] = n_maps

        return out

    def list_name_candidates(self, *, place_set_id: str, place_candidate_id: str) -> List[Dict[str, Any]]:
        with _conn_ctx() as conn:
            if not self._table_exists(conn, self.t_name_candidates):
                return []
            sql = f"""
            SELECT
              name_candidate_id::text,
              place_set_id::text,
              place_candidate_id::text,
              candidate_name,
              candidate_name_norm,
              source_kind,
              score,
              model_score,
              model_rank,
              selected_by_model,
              features,
              provenance,
              created_at
            FROM {_q_ident(self.t_name_candidates)}
            WHERE place_set_id::text = %s
              AND place_candidate_id::text = %s
            ORDER BY model_rank ASC NULLS LAST, model_score DESC NULLS LAST, score DESC NULLS LAST, created_at ASC
            """
            with conn.cursor() as cur:
                cur.execute(sql, (place_set_id, place_candidate_id))
                return list(cur.fetchall() or [])

    def _insert_name_feedback(
        self,
        conn,
        *,
        place_set_id: str,
        place_candidate_id: str,
        chosen_name_candidate_id: Optional[str],
        chosen_name: str,
        chosen_source: str,
        rejected_name_candidate_ids: Optional[List[str]] = None,
        reviewer: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not self._table_exists(conn, self.t_name_feedback):
            raise RuntimeError(f"Missing table: {self.t_name_feedback}. Run migrations.")
        rej = rejected_name_candidate_ids or []
        chosen_norm = (chosen_name or "").strip().lower()
        sql = f"""
        INSERT INTO {_q_ident(self.t_name_feedback)}
          (place_set_id, place_candidate_id, chosen_name_candidate_id, chosen_name, chosen_name_norm,
           chosen_source, rejected_name_candidate_ids, reviewer, context)
        VALUES
          (%s, %s, %s, %s, %s, %s, %s::uuid[], %s, %s::jsonb)
        RETURNING feedback_id::text, created_at
        """
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    place_set_id,
                    place_candidate_id,
                    chosen_name_candidate_id,
                    chosen_name,
                    chosen_norm,
                    chosen_source,
                    self._uuid_array_literal(rej),
                    reviewer,
                    psycopg2.extras.Json(context or {}),
                ),
            )
            row = cur.fetchone() or {}
        return dict(row)

    def choose_name_candidate(
        self,
        *,
        place_set_id: str,
        place_candidate_id: str,
        name_candidate_id: str,
        rejected_name_candidate_ids: Optional[List[str]] = None,
        reviewer: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with _conn_ctx() as conn:
            if not self._table_exists(conn, self.t_name_candidates):
                raise RuntimeError(f"Missing table: {self.t_name_candidates}. Run migrations.")
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT candidate_name
                    FROM {_q_ident(self.t_name_candidates)}
                    WHERE name_candidate_id::text = %s
                      AND place_set_id::text = %s
                      AND place_candidate_id::text = %s
                    LIMIT 1
                    """,
                    (name_candidate_id, place_set_id, place_candidate_id),
                )
                row = cur.fetchone()
            if not row:
                raise RuntimeError("name_candidate_id not found for this place candidate")

            fb = self._insert_name_feedback(
                conn,
                place_set_id=place_set_id,
                place_candidate_id=place_candidate_id,
                chosen_name_candidate_id=name_candidate_id,
                chosen_name=str(row.get("candidate_name") if isinstance(row, dict) else row[0]),
                chosen_source="user_pick",
                rejected_name_candidate_ids=rejected_name_candidate_ids,
                reviewer=reviewer,
                context=context,
            )
            return {"ok": True, "mode": "user_pick", **fb}

    def create_custom_name_candidate(
        self,
        *,
        place_set_id: str,
        place_candidate_id: str,
        custom_name: str,
        rejected_name_candidate_ids: Optional[List[str]] = None,
        reviewer: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        custom = (custom_name or "").strip()
        if not custom:
            raise RuntimeError("custom_name is required")
        with _conn_ctx() as conn:
            if not self._table_exists(conn, self.t_name_candidates):
                raise RuntimeError(f"Missing table: {self.t_name_candidates}. Run migrations.")
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {_q_ident(self.t_name_candidates)}
                      (place_set_id, place_candidate_id, candidate_name, candidate_name_norm,
                       source_kind, score, model_score, model_rank, selected_by_model, features, provenance)
                    VALUES
                      (%s, %s, %s, geo_work.normalize_alias(%s), 'custom', 1.0, 1.0, 1, false,
                       %s::jsonb, %s::jsonb)
                    ON CONFLICT (place_candidate_id, candidate_name_norm)
                    DO UPDATE SET
                      candidate_name = EXCLUDED.candidate_name,
                      source_kind = 'custom',
                      score = 1.0,
                      model_score = 1.0
                    RETURNING name_candidate_id::text
                    """,
                    (
                        place_set_id,
                        place_candidate_id,
                        custom,
                        custom,
                        psycopg2.extras.Json({"source_weight": 0.95, "quality": 1.0, "freq": 1}),
                        psycopg2.extras.Json({"inserted_by": "ui_custom"}),
                    ),
                )
                row = cur.fetchone() or {}
                chosen_id = str(row.get("name_candidate_id") if isinstance(row, dict) else row[0])

            fb = self._insert_name_feedback(
                conn,
                place_set_id=place_set_id,
                place_candidate_id=place_candidate_id,
                chosen_name_candidate_id=chosen_id,
                chosen_name=custom,
                chosen_source="custom",
                rejected_name_candidate_ids=rejected_name_candidate_ids,
                reviewer=reviewer,
                context=context,
            )
            return {"ok": True, "mode": "custom", "name_candidate_id": chosen_id, **fb}

    def apply_bulk_candidate_reviews(
        self,
        *,
        place_set_id: str,
        reviews: List[Dict[str, Any]],
        reviewer: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Fast-path reviewer for workspace cards.
        Each review row must include: place_candidate_id, name_candidate_id, chosen_place_type.
        Custom names are intentionally not supported here; use single-card mode for custom.
        """
        rows = list(reviews or [])
        out: Dict[str, Any] = {
            "ok": True,
            "total": len(rows),
            "saved": 0,
            "skipped": 0,
            "failed": 0,
            "errors": [],
        }
        if not rows:
            return out

        allowed_types = {"STOP", "POI", "STATION", "TERMINAL", "OTHER"}
        base_ctx = dict(context or {})
        if "source" not in base_ctx:
            base_ctx["source"] = "phase2_bulk_review"

        with _conn_ctx() as conn:
            if not self._table_exists(conn, self.t_name_candidates):
                raise RuntimeError(f"Missing table: {self.t_name_candidates}. Run migrations.")
            if not self._table_exists(conn, self.t_name_feedback):
                raise RuntimeError(f"Missing table: {self.t_name_feedback}. Run migrations.")
            if not self._table_exists(conn, self.t_poi_stop_feedback):
                raise RuntimeError(f"Missing table: {self.t_poi_stop_feedback}. Run migrations.")

            candidate_ids_by_place: Dict[str, List[str]] = {}

            with conn.cursor() as cur:
                for idx, item in enumerate(rows):
                    place_candidate_id = str(item.get("place_candidate_id") or "").strip()
                    name_candidate_id = str(item.get("name_candidate_id") or "").strip()
                    chosen_type = str(item.get("chosen_place_type") or "").upper().strip()

                    if not place_candidate_id or not name_candidate_id or not chosen_type:
                        out["skipped"] += 1
                        continue
                    if chosen_type not in allowed_types:
                        out["failed"] += 1
                        out["errors"].append(
                            {
                                "index": idx,
                                "place_candidate_id": place_candidate_id,
                                "error": f"invalid chosen_place_type: {chosen_type}",
                            }
                        )
                        continue

                    cur.execute(
                        f"""
                        SELECT candidate_name
                        FROM {_q_ident(self.t_name_candidates)}
                        WHERE name_candidate_id::text = %s
                          AND place_set_id::text = %s
                          AND place_candidate_id::text = %s
                        LIMIT 1
                        """,
                        (name_candidate_id, place_set_id, place_candidate_id),
                    )
                    row = cur.fetchone()
                    if not row:
                        out["failed"] += 1
                        out["errors"].append(
                            {
                                "index": idx,
                                "place_candidate_id": place_candidate_id,
                                "error": "name_candidate_id not found for this place candidate",
                            }
                        )
                        continue

                    candidate_name = str(row.get("candidate_name") if isinstance(row, dict) else row[0])
                    if place_candidate_id not in candidate_ids_by_place:
                        cur.execute(
                            f"""
                            SELECT name_candidate_id::text AS name_candidate_id
                            FROM {_q_ident(self.t_name_candidates)}
                            WHERE place_set_id::text = %s
                              AND place_candidate_id::text = %s
                            """,
                            (place_set_id, place_candidate_id),
                        )
                        candidate_ids_by_place[place_candidate_id] = [
                            str(r.get("name_candidate_id") or "")
                            for r in (cur.fetchall() or [])
                            if r.get("name_candidate_id")
                        ]
                    rejected_ids = [
                        cid for cid in candidate_ids_by_place.get(place_candidate_id, []) if cid != name_candidate_id
                    ]

                    ctx = dict(base_ctx)
                    ctx["bulk_index"] = idx
                    self._insert_name_feedback(
                        conn,
                        place_set_id=place_set_id,
                        place_candidate_id=place_candidate_id,
                        chosen_name_candidate_id=name_candidate_id,
                        chosen_name=candidate_name,
                        chosen_source="user_pick",
                        rejected_name_candidate_ids=rejected_ids,
                        reviewer=reviewer,
                        context=ctx,
                    )

                    cur.execute(
                        f"""
                        INSERT INTO {_q_ident(self.t_poi_stop_feedback)}
                          (place_set_id, place_candidate_id, chosen_place_type, chosen_source, reviewer, context)
                        VALUES (%s, %s, %s, 'user_pick', %s, %s::jsonb)
                        """,
                        (
                            place_set_id,
                            place_candidate_id,
                            chosen_type,
                            reviewer,
                            psycopg2.extras.Json(ctx),
                        ),
                    )
                    out["saved"] += 1

        out["ok"] = out["failed"] == 0
        if len(out["errors"]) > 20:
            out["errors"] = list(out["errors"][:20]) + [{"error": "truncated"}]
        return out

    def get_latest_name_feedback(self, *, place_set_id: str, place_candidate_id: str) -> Dict[str, Any]:
        with _conn_ctx() as conn:
            if not self._table_exists(conn, self.t_name_feedback):
                return {}
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                      feedback_id::text,
                      place_set_id::text,
                      place_candidate_id::text,
                      chosen_name_candidate_id::text,
                      chosen_name,
                      chosen_source,
                      rejected_name_candidate_ids,
                      reviewer,
                      context,
                      created_at
                    FROM {_q_ident(self.t_name_feedback)}
                    WHERE place_set_id::text = %s
                      AND place_candidate_id::text = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (place_set_id, place_candidate_id),
                )
                row = cur.fetchone()
            if not row:
                return {}
            out = dict(row)
            rej = out.get("rejected_name_candidate_ids")
            out["rejected_name_candidate_ids"] = [str(x) for x in list(rej)] if rej is not None else []
            return out

    def list_name_feedback_events(self, *, place_set_id: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        with _conn_ctx() as conn:
            if not self._table_exists(conn, self.t_name_feedback):
                return []
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                      feedback_id::text,
                      place_set_id::text,
                      place_candidate_id::text,
                      chosen_name,
                      chosen_source,
                      rejected_name_candidate_ids,
                      reviewer,
                      context,
                      created_at
                    FROM {_q_ident(self.t_name_feedback)}
                    WHERE (%s IS NULL OR place_set_id::text = %s)
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (place_set_id, place_set_id, int(limit)),
                )
                rows = list(cur.fetchall() or [])
            for r in rows:
                rej = r.get("rejected_name_candidate_ids")
                r["rejected_name_candidate_ids"] = [str(x) for x in list(rej)] if rej is not None else []
            return rows

    def save_place_type_feedback(
        self,
        *,
        place_set_id: str,
        place_candidate_id: str,
        chosen_place_type: str,
        reviewer: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        chosen = str(chosen_place_type or "").upper().strip()
        allowed = {"STOP", "POI", "STATION", "TERMINAL", "OTHER"}
        if chosen not in allowed:
            raise RuntimeError(f"Invalid chosen_place_type: {chosen}")
        with _conn_ctx() as conn:
            if not self._table_exists(conn, self.t_poi_stop_feedback):
                raise RuntimeError(f"Missing table: {self.t_poi_stop_feedback}. Run migrations.")
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {_q_ident(self.t_poi_stop_feedback)}
                      (place_set_id, place_candidate_id, chosen_place_type, chosen_source, reviewer, context)
                    VALUES (%s, %s, %s, 'user_pick', %s, %s::jsonb)
                    RETURNING feedback_id::text, created_at
                    """,
                    (place_set_id, place_candidate_id, chosen, reviewer, psycopg2.extras.Json(context or {})),
                )
                row = cur.fetchone() or {}
            return {"ok": True, "chosen_place_type": chosen, **dict(row)}

    def get_latest_place_type_feedback(self, *, place_set_id: str, place_candidate_id: str) -> Dict[str, Any]:
        with _conn_ctx() as conn:
            if not self._table_exists(conn, self.t_poi_stop_feedback):
                return {}
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                      feedback_id::text,
                      place_set_id::text,
                      place_candidate_id::text,
                      chosen_place_type,
                      chosen_source,
                      reviewer,
                      context,
                      created_at
                    FROM {_q_ident(self.t_poi_stop_feedback)}
                    WHERE place_set_id::text = %s
                      AND place_candidate_id::text = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (place_set_id, place_candidate_id),
                )
                row = cur.fetchone()
            return dict(row) if row else {}

    def list_place_type_feedback_events(self, *, place_set_id: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        with _conn_ctx() as conn:
            if not self._table_exists(conn, self.t_poi_stop_feedback):
                return []
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                      feedback_id::text,
                      place_set_id::text,
                      place_candidate_id::text,
                      chosen_place_type,
                      chosen_source,
                      reviewer,
                      context,
                      created_at
                    FROM {_q_ident(self.t_poi_stop_feedback)}
                    WHERE (%s IS NULL OR place_set_id::text = %s)
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (place_set_id, place_set_id, int(limit)),
                )
                return list(cur.fetchall() or [])

    # ===================================================================
    # Block 2: Cleanup Infrastructure
    # ===================================================================

    def purge_deprecated_places(self, *, dry_run: bool = True) -> Dict[str, Any]:
        """Delete all deprecated places. CASCADE handles aliases + embeddings."""
        with _conn_ctx() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*)::int AS cnt FROM geo_prod.places WHERE status = 'deprecated'")
                n_deprecated = cur.fetchone()["cnt"]

                cur.execute(
                    """SELECT COUNT(*)::int AS cnt FROM geo_prod.place_aliases a
                       JOIN geo_prod.places p ON p.place_id = a.place_id
                       WHERE p.status = 'deprecated'"""
                )
                n_aliases = cur.fetchone()["cnt"]

                cur.execute(
                    """SELECT COUNT(*)::int AS cnt FROM geo_prod.place_alias_embeddings e
                       JOIN geo_prod.place_aliases a ON a.alias_id = e.alias_id
                       JOIN geo_prod.places p ON p.place_id = a.place_id
                       WHERE p.status = 'deprecated'"""
                )
                n_embeddings = cur.fetchone()["cnt"]

                cur.execute(
                    """SELECT COUNT(*)::int AS cnt FROM geo_prod.node_place_map m
                       JOIN geo_prod.places p ON p.place_id = m.place_id
                       WHERE p.status = 'deprecated'"""
                )
                n_mappings = cur.fetchone()["cnt"]

                result = {
                    "deprecated_places": n_deprecated,
                    "cascade_aliases": n_aliases,
                    "cascade_embeddings": n_embeddings,
                    "blocking_node_mappings": n_mappings,
                    "est_freed_mb": round(n_embeddings * 1536 / 1024 / 1024, 1),
                    "dry_run": dry_run,
                    "deleted": 0,
                    "mappings_removed": 0,
                }

                if not dry_run and n_deprecated > 0:
                    if n_mappings > 0:
                        cur.execute(
                            """DELETE FROM geo_prod.node_place_map
                               WHERE place_id IN (
                                   SELECT place_id FROM geo_prod.places WHERE status = 'deprecated'
                               )"""
                        )
                        result["mappings_removed"] = cur.rowcount

                    cur.execute("DELETE FROM geo_prod.places WHERE status = 'deprecated'")
                    result["deleted"] = cur.rowcount
                    conn.commit()

                return result

    def purge_stale_candidate_sets(self, *, max_age_days: int = 0, dry_run: bool = True) -> Dict[str, Any]:
        """Delete candidate sets that were never approved."""
        with _conn_ctx() as conn:
            with conn.cursor() as cur:
                age_filter = ""
                params: list = []
                if max_age_days > 0:
                    age_filter = "AND pcs.created_at < now() - interval '%s days'"
                    params.append(max_age_days)

                cur.execute(
                    f"""SELECT pcs.place_set_id::text, pcs.context_key, pcs.created_at::text
                        FROM geo_work.place_candidate_sets pcs
                        WHERE pcs.place_set_id NOT IN (
                            SELECT DISTINCT chosen_set_id FROM geo_work.selection_log
                            WHERE chosen_set_id IS NOT NULL
                        )
                        {age_filter}
                        ORDER BY pcs.created_at""",
                    params,
                )
                stale = [dict(r) for r in cur.fetchall()]
                stale_ids = [s["place_set_id"] for s in stale]

                n_candidates = 0
                if stale_ids:
                    cur.execute(
                        "SELECT COUNT(*)::int AS cnt FROM geo_work.place_candidates WHERE place_set_id = ANY(%s::uuid[])",
                        (stale_ids,),
                    )
                    n_candidates = cur.fetchone()["cnt"]

                result = {
                    "stale_sets": len(stale_ids),
                    "stale_candidates": n_candidates,
                    "set_details": stale[:10],
                    "dry_run": dry_run,
                    "deleted_sets": 0,
                }

                if not dry_run and stale_ids:
                    for sid in stale_ids:
                        cur.execute(
                            """UPDATE geo_work.selection_log
                               SET rejected_set_ids = array_remove(rejected_set_ids, %s::uuid)
                               WHERE %s::uuid = ANY(rejected_set_ids)""",
                            (sid, sid),
                        )
                    cur.execute(
                        "DELETE FROM geo_work.place_candidate_sets WHERE place_set_id = ANY(%s::uuid[])",
                        (stale_ids,),
                    )
                    result["deleted_sets"] = cur.rowcount
                    conn.commit()

                return result

    def detect_active_orphans(self, *, limit: int = 200) -> list:
        """Find active places with no node mapping."""
        with _conn_ctx() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT p.place_id::text, p.canonical_name, p.place_type, p.created_at::text
                       FROM geo_prod.places p
                       LEFT JOIN geo_prod.node_place_map m ON m.place_id = p.place_id
                       WHERE p.status = 'active' AND m.place_id IS NULL
                       LIMIT %s""",
                    (limit,),
                )
                return [dict(r) for r in cur.fetchall()]

    def deprecate_active_orphans(self, *, dry_run: bool = True) -> Dict[str, Any]:
        """Set status='deprecated' for active places with no node mapping."""
        with _conn_ctx() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT COUNT(*)::int AS cnt FROM geo_prod.places p
                       LEFT JOIN geo_prod.node_place_map m ON m.place_id = p.place_id
                       WHERE p.status = 'active' AND m.place_id IS NULL"""
                )
                n_orphans = cur.fetchone()["cnt"]

                result = {"active_orphans": n_orphans, "dry_run": dry_run, "deprecated": 0}

                if not dry_run and n_orphans > 0:
                    cur.execute(
                        """UPDATE geo_prod.places
                           SET status = 'deprecated', updated_at = now()
                           WHERE status = 'active'
                             AND place_id NOT IN (SELECT place_id FROM geo_prod.node_place_map)"""
                    )
                    result["deprecated"] = cur.rowcount
                    conn.commit()

                return result

    def cleanup_summary(self) -> Dict[str, Any]:
        """Get current cleanup status: counts of deprecated, orphans, stale sets."""
        with _conn_ctx() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*)::int AS cnt FROM geo_prod.places WHERE status = 'deprecated'")
                n_deprecated = cur.fetchone()["cnt"]

                cur.execute(
                    """SELECT COUNT(*)::int AS cnt FROM geo_prod.places p
                       LEFT JOIN geo_prod.node_place_map m ON m.place_id = p.place_id
                       WHERE p.status = 'active' AND m.place_id IS NULL"""
                )
                n_orphans = cur.fetchone()["cnt"]

                cur.execute(
                    """SELECT COUNT(*)::int AS cnt FROM geo_work.place_candidate_sets pcs
                       WHERE pcs.place_set_id NOT IN (
                           SELECT DISTINCT chosen_set_id FROM geo_work.selection_log
                           WHERE chosen_set_id IS NOT NULL
                       )"""
                )
                n_stale_sets = cur.fetchone()["cnt"]

                cur.execute("SELECT COUNT(*)::int AS cnt FROM geo_prod.places WHERE status = 'active'")
                n_active = cur.fetchone()["cnt"]

                cur.execute("SELECT COUNT(*)::int AS cnt FROM geo_prod.places")
                n_total = cur.fetchone()["cnt"]

                cur.execute(
                    """SELECT COUNT(*)::int AS cnt FROM geo_prod.place_alias_embeddings e
                       JOIN geo_prod.place_aliases a ON a.alias_id = e.alias_id
                       JOIN geo_prod.places p ON p.place_id = a.place_id
                       WHERE p.status = 'deprecated'"""
                )
                n_dep_embeddings = cur.fetchone()["cnt"]

                return {
                    "total_places": n_total,
                    "active_places": n_active,
                    "deprecated_places": n_deprecated,
                    "active_orphans": n_orphans,
                    "stale_candidate_sets": n_stale_sets,
                    "reclaimable_embedding_mb": round(n_dep_embeddings * 1536 / 1024 / 1024, 1),
                }

    def check_cross_phase_references(self, place_id: str) -> Dict[str, int]:
        """Check if a place is referenced by Phase 3 or Phase 4."""
        refs: Dict[str, int] = {}
        with _conn_ctx() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        """SELECT COUNT(*)::int AS cnt FROM route_prod.routes r
                           WHERE EXISTS (
                               SELECT 1 FROM geo_prod.node_place_map npm
                               WHERE npm.place_id = %s::uuid
                                 AND npm.node_id = ANY(r.stop_node_ids)
                           )""",
                        (place_id,),
                    )
                    refs["phase3_routes"] = cur.fetchone()["cnt"]
                except Exception:
                    refs["phase3_routes"] = -1

                try:
                    cur.execute(
                        "SELECT COUNT(*)::int AS cnt FROM semantics.route_name_evidence WHERE place_id = %s::uuid",
                        (place_id,),
                    )
                    refs["phase4_evidence"] = cur.fetchone()["cnt"]
                except Exception:
                    refs["phase4_evidence"] = -1

        return refs

    def get_name_duplication_stats(self, *, min_count: int = 50, limit: int = 30) -> list:
        """Top duplicated canonical names among active places."""
        with _conn_ctx() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT canonical_name, place_type, COUNT(*)::int AS cnt
                       FROM geo_prod.places
                       WHERE status = 'active'
                         AND canonical_name IS NOT NULL
                       GROUP BY canonical_name, place_type
                       HAVING COUNT(*) >= %s
                       ORDER BY cnt DESC
                       LIMIT %s""",
                    (min_count, limit),
                )
                return [dict(r) for r in cur.fetchall()]
