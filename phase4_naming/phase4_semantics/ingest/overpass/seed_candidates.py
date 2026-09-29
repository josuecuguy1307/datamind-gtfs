# phase4_semantics/ingest/overpass/seed_candidates.py
from __future__ import annotations

import argparse
import json
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import UUID
from urllib.parse import quote_plus
from phase4_semantics.common.db import get_db_dsn
import psycopg2
import psycopg2.extras
import requests


# ------------------------------------------------------------
# Basics
# ------------------------------------------------------------

DEFAULT_OVERPASS_URL = os.getenv("OVERPASS_URL", "http://127.0.0.1:12346/api/interpreter")

# Try common DSN env names (you can change this to match your project)
# NOTE:
# Do NOT hardcode a postgres/postgres fallback. That silently breaks on machines
# where the role/database is different (your case).
def _supabase_dsn_from_env() -> str | None:
    host = os.getenv("SUPABASE_DB_HOST")
    if not host:
        return None
    port = os.getenv("SUPABASE_DB_PORT", "5432")
    name = os.getenv("SUPABASE_DB_NAME", "postgres")
    user = os.getenv("SUPABASE_DB_USER", "postgres")
    password = os.getenv("SUPABASE_DB_PASSWORD", "")
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{name}"
        "?sslmode=require"
    )


DEFAULT_DSN = (
    os.getenv("DATABASE_URL")
    or os.getenv("DB_DSN")
    or os.getenv("PG_DSN")
    or os.getenv("POSTGRES_DSN")
    or _supabase_dsn_from_env()
    or None
)



def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_jsonl(path: str, rows: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            out.append(json.loads(s))
    return out


def _cap01(v: float) -> float:
    if v < 0:
        return 0.0
    if v > 1:
        return 1.0
    return v


# ------------------------------------------------------------
# DB helpers (introspective, robust)
# ------------------------------------------------------------

def _conn(dsn: str | None = None):
    return psycopg2.connect(dsn or get_db_dsn())


def _columns(cur, schema: str, table: str) -> Set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        """,
        (schema, table),
    )
    rows = cur.fetchall() or []
    out: Set[str] = set()
    for r in rows:
        # RealDictCursor returns dict-like rows
        if isinstance(r, dict):
            v = r.get("column_name")
        else:
            v = r[0]
        if v:
            out.add(str(v))
    return out

     

def _table_exists(cur, schema: str, table: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = %s AND table_name = %s
        """,
        (schema, table),
    )
    return cur.fetchone() is not None


def _q1(cur, sql: str, params=()):
    cur.execute(sql, params)
    r = cur.fetchone()
    return dict(r) if r else None


def _qall(cur, sql: str, params=()):
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


# ------------------------------------------------------------
# Overpass helpers
# ------------------------------------------------------------

def overpass_query(url: str, query: str, timeout_s: int = 60, retries: int = 2) -> Dict[str, Any]:
    """
    Sends Overpass QL query, returns JSON dict.
    """
    last_err = None
    for i in range(retries + 1):
        try:
            resp = requests.post(url, data=query.encode("utf-8"), timeout=timeout_s)
            if resp.status_code != 200:
                raise RuntimeError(f"Overpass HTTP {resp.status_code}: {resp.text[:300]}")
            return resp.json()
        except Exception as e:
            last_err = e
            # small backoff
            time.sleep(0.7 * (i + 1))
    raise RuntimeError(f"Overpass query failed after retries: {last_err}")


def build_candidate_relations_query(node_ids: List[int], route_regex: str) -> str:
    """
    Candidate generation:
      take some nodes -> find relations that contain them

    route_regex example: "bus|trolleybus|tram"
    """
    # node(id:1,2,3,...) syntax
    node_id_list = ",".join(str(x) for x in node_ids)

    return f"""
[out:json][timeout:25];
node(id:{node_id_list});
rel(bn)["type"="route"]["route"~"{route_regex}"];
out tags;
"""


def build_relation_members_query(relation_id: int) -> str:
    """
    Fetch relation + members fully (nodes and ways).
    """
    return f"""
[out:json][timeout:60];
relation({relation_id});
(._;>;);
out body;
"""


def extract_relation_tags(overpass_resp: Dict[str, Any], relation_id: int) -> Dict[str, str]:
    for el in overpass_resp.get("elements", []):
        if el.get("type") == "relation" and int(el.get("id", -1)) == int(relation_id):
            tags = el.get("tags") or {}
            return {str(k): str(v) for k, v in tags.items()}
    return {}


def extract_relation_member_node_ids(overpass_resp: Dict[str, Any]) -> Set[int]:
    """
    Collect all node ids referenced by the relation response:
      - nodes as elements
      - nodes referenced in ways (the "nodes" array)
    """
    out: Set[int] = set()

    for el in overpass_resp.get("elements", []):
        t = el.get("type")
        if t == "node" and "id" in el:
            try:
                out.add(int(el["id"]))
            except Exception:
                pass

        if t == "way":
            # ways contain a list of node ids
            node_list = el.get("nodes") or []
            for nid in node_list:
                try:
                    out.add(int(nid))
                except Exception:
                    pass

    return out


# ------------------------------------------------------------
# Core: route_id -> stop_node_ids -> OSM node ids
# ------------------------------------------------------------


def load_osm_node_ids_from_stop_prior(cur, route_id: str) -> List[int]:
    """
    Option B:
    Get OSM node IDs directly from Phase 3 stop prior table:
      route_work.relation_stop_prior

    Uses:
      - member_type='node' AND osm_ref -> OSM node id
      - fallback to legacy osm_node_id if present
    """
    if not _table_exists(cur, "route_work", "relation_stop_prior"):
        raise RuntimeError("Missing table route_work.relation_stop_prior (Phase 3 stop prior not available)")

    cols = _columns(cur, "route_work", "relation_stop_prior")

    if "route_id" not in cols:
        raise RuntimeError("route_work.relation_stop_prior missing route_id column")

    # We need at least one of these
    has_member_type = "member_type" in cols
    has_osm_ref = "osm_ref" in cols
    has_legacy = "osm_node_id" in cols

    if not (has_osm_ref or has_legacy):
        raise RuntimeError("route_work.relation_stop_prior missing osm_ref/osm_node_id columns")

    cur.execute(
        """
        SELECT seq, member_type, osm_ref, osm_node_id
        FROM route_work.relation_stop_prior
        WHERE route_id = %s
        ORDER BY seq ASC
        """,
        (route_id,),
    )
    rows = cur.fetchall() or []

    node_ids: List[int] = []
    for r in rows:
        # r is dict-like because RealDictCursor
        mtype = (r.get("member_type") or "").lower()
        osm_ref = r.get("osm_ref")
        legacy = r.get("osm_node_id")

        # Prefer member_type=node + osm_ref
        if mtype == "node" and osm_ref is not None:
            try:
                node_ids.append(int(osm_ref))
            except Exception:
                pass
            continue

        # Fallback: legacy osm_node_id (older prior schema)
        if legacy is not None:
            try:
                node_ids.append(int(legacy))
            except Exception:
                pass

    # de-dup while keeping order
    seen = set()
    out: List[int] = []
    for nid in node_ids:
        if nid in seen:
            continue
        seen.add(nid)
        out.append(nid)

    return out

# ------------------------------------------------------------
# Scoring: intersection
# ------------------------------------------------------------

def score_relation(
    relation_member_nodes: Set[int],
    stop_osm_nodes: Set[int],
) -> Dict[str, Any]:
    """
    Compute intersection features used for picking the best seed.
    """
    if not stop_osm_nodes:
        return {
            "overlap_count": 0,
            "overlap_ratio_stop": 0.0,
            "overlap_ratio_relation": 0.0,
            "relation_member_nodes": len(relation_member_nodes),
        }

    overlap = relation_member_nodes.intersection(stop_osm_nodes)
    overlap_count = len(overlap)

    overlap_ratio_stop = overlap_count / max(1, len(stop_osm_nodes))
    overlap_ratio_relation = overlap_count / max(1, len(relation_member_nodes))

    return {
        "overlap_count": overlap_count,
        "overlap_ratio_stop": float(_cap01(overlap_ratio_stop)),
        "overlap_ratio_relation": float(_cap01(overlap_ratio_relation)),
        "relation_member_nodes": len(relation_member_nodes),
    }


def tag_richness(tags: Dict[str, str]) -> int:
    """
    Tie-breaker: more useful tags = more confidence.
    """
    keys = ["ref", "name", "operator", "network", "from", "to"]
    return sum(1 for k in keys if tags.get(k))


# ------------------------------------------------------------
# Main pipeline
# ------------------------------------------------------------

def seed_candidates_for_route(
    dsn: str,
    route_id: str,
    overpass_url: str,
    *,
    route_regex: str = "bus|trolleybus|tram",
    max_stop_nodes: int = 60,
    max_relations_fetch: int = 12,
    include_best_raw: bool = True,
    random_seed: int = 13,
) -> Dict[str, Any]:
    """
    Returns a dict with:
      - best_candidate (or None)
      - candidates list
      - debug summary
    """
    random.seed(random_seed)

    with _conn(dsn) as conn:
     with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        stop_osm_ids = load_osm_node_ids_from_stop_prior(cur, route_id)


  

    if not stop_osm_ids:
        return {
            "route_id": route_id,
            "best_candidate": None,
            "candidates": [],
            "debug": {
                "reason": "no_osm_node_ids_found_for_stop_nodes",
                "stop_osm_ids_count": 0,
            },
        }

    # sample nodes to avoid massive query
    sampled = stop_osm_ids
    if len(sampled) > max_stop_nodes:
        sampled = random.sample(sampled, max_stop_nodes)

    # 1) Candidate relations (cheap)
    q = build_candidate_relations_query(sampled, route_regex=route_regex)
    cand_resp = overpass_query(overpass_url, q, timeout_s=40, retries=2)

    relation_ids: List[int] = []
    relation_tags_map: Dict[int, Dict[str, str]] = {}

    for el in cand_resp.get("elements", []):
        if el.get("type") != "relation":
            continue
        rid = el.get("id")
        if rid is None:
            continue
        try:
            rid_int = int(rid)
        except Exception:
            continue
        relation_ids.append(rid_int)
        tags = el.get("tags") or {}
        relation_tags_map[rid_int] = {str(k): str(v) for k, v in tags.items()}

    relation_ids = list(sorted(set(relation_ids)))

    if not relation_ids:
        return {
            "route_id": route_id,
            "best_candidate": None,
            "candidates": [],
            "debug": {
                "stop_osm_ids_count": len(stop_osm_ids),
                "sampled_count": len(sampled),
                "candidate_relations_found": len(relation_ids),
                "relations_fetched": len(relation_ids_sorted),
            },

        }

    # heuristic pre-sort (fetch only top relations to compute exact overlap)
    def _pre_score(rid: int) -> Tuple[int, int]:
        tags = relation_tags_map.get(rid, {})
        # prefer correct "route=bus" + tag richness
        rt = tags.get("route", "")
        is_bus = 1 if rt and any(x in rt.lower() for x in ["bus", "trolley", "tram"]) else 0
        return (is_bus, tag_richness(tags))

    relation_ids_sorted = sorted(relation_ids, key=_pre_score, reverse=True)
    relation_ids_sorted = relation_ids_sorted[:max_relations_fetch]

    # 2) Fetch members for each candidate and compute overlap
    stop_set = set(stop_osm_ids)

    candidates: List[Dict[str, Any]] = []
    best: Optional[Dict[str, Any]] = None

    for rid in relation_ids_sorted:
        members_q = build_relation_members_query(rid)
        try:
            rel_full = overpass_query(overpass_url, members_q, timeout_s=70, retries=1)
        except Exception as e:
            # skip bad relation fetch
            continue

        member_nodes = extract_relation_member_node_ids(rel_full)
        tags = extract_relation_tags(rel_full, rid) or relation_tags_map.get(rid, {})

        sc = score_relation(member_nodes, stop_set)

        cand = {
            "route_id": route_id,
            "relation_id": rid,
            "source_type": "osm_overpass_seed",
            "relation_tags": tags,
            "overlap_count": sc["overlap_count"],
            "overlap_ratio": sc["overlap_ratio_stop"],  # main ratio used as confidence_hint later
            "overlap_ratio_relation": sc["overlap_ratio_relation"],
            "relation_member_nodes": sc["relation_member_nodes"],
            "created_at": _now_iso(),
        }

        # include raw only for best by default (keeps file light)
        if include_best_raw:
            cand["_raw_relation_full"] = None  # filled if chosen as best

        candidates.append(cand)

        # pick best: (overlap_count, overlap_ratio_stop, tag_richness)
        if best is None:
            best = cand
        else:
            b_tags = best.get("relation_tags") or {}
            c_tags = tags or {}
            best_key = (
                int(best.get("overlap_count", 0)),
                float(best.get("overlap_ratio", 0.0)),
                tag_richness(b_tags),
            )
            cand_key = (
                int(cand.get("overlap_count", 0)),
                float(cand.get("overlap_ratio", 0.0)),
                tag_richness(c_tags),
            )
            if cand_key > best_key:
                best = cand

        # if raw is enabled, hold the raw for the best candidate
        if include_best_raw and best is cand:
            best["_raw_relation_full"] = rel_full

    # sort for output readability
    candidates = sorted(
        candidates,
        key=lambda x: (
            int(x.get("overlap_count", 0)),
            float(x.get("overlap_ratio", 0.0)),
            tag_richness(x.get("relation_tags") or {}),
        ),
        reverse=True,
    )

    # ensure best raw exists only for best
    if include_best_raw:
        for c in candidates:
            if c is best:
                continue
            if "_raw_relation_full" in c:
                c["_raw_relation_full"] = None

    return {
        "route_id": route_id,
        "best_candidate": best,
        "candidates": candidates,
        "debug": {
                "stop_osm_ids_count": len(stop_osm_ids),
                "sampled_count": len(sampled),
                "candidate_relations_found": len(relation_ids),
                "relations_fetched": len(relation_ids_sorted),
            },

    }


# ------------------------------------------------------------
# CLI
# ------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Overpass relations-first seed: maximize stop-node intersection -> seed candidates JSONL"
    )
    ap.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN")
    ap.add_argument("--overpass-url", default=DEFAULT_OVERPASS_URL, help="Overpass endpoint")
    ap.add_argument("--route-id", default=None, help="Single route_id (uuid)")
    ap.add_argument("--routes-jsonl", default=None, help="JSONL with {'route_id': ...} rows")
    ap.add_argument("--out", default="seed_candidates.jsonl", help="Output JSONL")
    ap.add_argument("--route-regex", default="bus|trolleybus|tram", help="Overpass filter for route tag")
    ap.add_argument("--max-stop-nodes", type=int, default=60, help="How many OSM stop nodes to sample")
    ap.add_argument("--max-relations-fetch", type=int, default=12, help="How many candidate relations to fetch fully")
    ap.add_argument("--include-best-raw", action="store_true", help="Include _raw_relation_full only for best candidate")
    ap.add_argument("--no-raw", action="store_true", help="Do not include any raw relation payload")
    ap.add_argument("--seed", type=int, default=13, help="Random seed for node sampling")

    args = ap.parse_args()

    if not args.route_id and not args.routes_jsonl:
        raise SystemExit("Provide --route-id or --routes-jsonl")

    include_best_raw = bool(args.include_best_raw) and (not args.no_raw)

    route_ids: List[str] = []
    if args.route_id:
        route_ids = [args.route_id.strip()]
    else:
        rows = _read_jsonl(args.routes_jsonl)
        for r in rows:
            rid = r.get("route_id")
            if rid:
                route_ids.append(str(rid).strip())

    out_rows: List[Dict[str, Any]] = []

    for rid in route_ids:
        res = seed_candidates_for_route(
            dsn=args.dsn,
            route_id=rid,
            overpass_url=args.overpass_url,
            route_regex=args.route_regex,
            max_stop_nodes=args.max_stop_nodes,
            max_relations_fetch=args.max_relations_fetch,
            include_best_raw=include_best_raw,
            random_seed=args.seed,
        )

        best = res.get("best_candidate")
        if best:
            # We output the BEST candidate as the "seed row" (this is what normalize_to_evidence expects)
            out_rows.append(best)

        # optional: also dump the full candidate list into a side file
        # (not required for pipeline)
        # You can uncomment if you want:
        # _write_jsonl(args.out.replace(".jsonl", "_all_candidates.jsonl"), res["candidates"])

        print(
            f"✅ route_id={rid} | best_relation={best.get('relation_id') if best else None} "
            f"| overlap={best.get('overlap_count') if best else 0} "
            f"| ratio={best.get('overlap_ratio') if best else 0.0}"
        )

    _write_jsonl(args.out, out_rows)
    print(f"\n✅ wrote {len(out_rows)} best seed candidates -> {args.out}")


if __name__ == "__main__":
    main()

# ------------------------------------------------------------
# Orchestrator-facing wrapper (what run_phase4.py expects)
# ------------------------------------------------------------

def seed_overpass_candidates(
    route_id: str,
    sample_points: Optional[List[Tuple[float, float]]] = None,
    sample_version: str = "v1",
    *,
    dsn: str | None = None,
    overpass_url: str = DEFAULT_OVERPASS_URL,
    route_regex: str = "bus|trolleybus|tram",
    max_stop_nodes: int = 60,
    max_relations_fetch: int = 12,
    include_best_raw: bool = False,
    random_seed: int = 13,
) -> List[Dict[str, Any]]:
    """
    Deterministic Phase-4 seed strategy:
      1) Reuse Phase-3 chosen OSM relation id (no rediscovery).
      2) Fallback: endpoint-node intersection (first/last stop in latest sequence).

    sample_points/sample_version are kept for orchestrator compatibility.
    """
    dsn_eff = dsn or DEFAULT_DSN or get_db_dsn()

    def _persist_seed_if_possible(cur, cand: Dict[str, Any]) -> None:
        if not _table_exists(cur, "semantics", "route_seed_candidates"):
            return
        cols = _columns(cur, "semantics", "route_seed_candidates")
        if not {"route_id", "sample_version", "relation_id"}.issubset(cols):
            return

        values: Dict[str, Any] = {
            "route_id": route_id,
            "sample_version": sample_version,
            "relation_id": int(cand.get("relation_id")),
        }
        if "overlap_ratio" in cols:
            values["overlap_ratio"] = float(cand.get("overlap_ratio") or 0.0)
        if "overlap_count" in cols:
            values["overlap_count"] = int(cand.get("overlap_count") or 0)
        if "source_type" in cols:
            values["source_type"] = str(cand.get("source_type") or "osm_overpass_seed")
        if "relation_tags" in cols:
            values["relation_tags"] = psycopg2.extras.Json(cand.get("relation_tags") or {})
        if "overpass_raw" in cols:
            values["overpass_raw"] = psycopg2.extras.Json(cand.get("_raw_relation_full") or {})
        if "created_at" in cols:
            values["created_at"] = _now_iso()

        c_names = list(values.keys())
        placeholders = ", ".join(["%s"] * len(c_names))
        col_sql = ", ".join(c_names)
        params = [values[c] for c in c_names]

        updatable = [c for c in c_names if c not in ("route_id", "sample_version", "relation_id")]
        if updatable:
            update_sql = ", ".join([f"{c}=EXCLUDED.{c}" for c in updatable])
            sql = (
                f"INSERT INTO semantics.route_seed_candidates ({col_sql}) VALUES ({placeholders}) "
                f"ON CONFLICT (route_id, sample_version, relation_id) DO UPDATE SET {update_sql}"
            )
        else:
            sql = (
                f"INSERT INTO semantics.route_seed_candidates ({col_sql}) VALUES ({placeholders}) "
                f"ON CONFLICT (route_id, sample_version, relation_id) DO NOTHING"
            )
        cur.execute(sql, params)

    def _candidate_from_relation(
        *,
        rid: int,
        route_id_: str,
        source_type: str,
        overpass_raw: Optional[Dict[str, Any]],
        overlap_count: int = 0,
        overlap_ratio: float = 0.0,
    ) -> Dict[str, Any]:
        tags = extract_relation_tags(overpass_raw or {}, rid)
        return {
            "route_id": route_id_,
            "relation_id": int(rid),
            "source_type": source_type,
            "relation_tags": tags,
            "overlap_count": int(overlap_count),
            "overlap_ratio": float(overlap_ratio),
            "overlap_ratio_relation": 0.0,
            "relation_member_nodes": 0,
            "created_at": _now_iso(),
            "_raw_relation_full": overpass_raw if include_best_raw else None,
        }

    def _load_phase3_chosen_relation_id(cur, rid_route: str) -> Optional[int]:
        # Preferred: route_raw.route_jobs.chosen_osm_relation_id
        row = _q1(
            cur,
            """
            SELECT chosen_osm_relation_id
            FROM route_raw.route_jobs
            WHERE route_id = %s
            """,
            (rid_route,),
        )
        if row and row.get("chosen_osm_relation_id") is not None:
            try:
                return int(row["chosen_osm_relation_id"])
            except Exception:
                pass

        # Fallback: route_prod.routes.osm_relation_id (if present in your schema)
        if _table_exists(cur, "route_prod", "routes"):
            cols = _columns(cur, "route_prod", "routes")
            if "osm_relation_id" in cols:
                row2 = _q1(
                    cur,
                    """
                    SELECT osm_relation_id
                    FROM route_prod.routes
                    WHERE route_id = %s
                    """,
                    (rid_route,),
                )
                if row2 and row2.get("osm_relation_id") is not None:
                    try:
                        return int(row2["osm_relation_id"])
                    except Exception:
                        pass
        return None

    def _load_endpoints_osm_node_ids(cur, rid_route: str) -> Tuple[Optional[int], Optional[int]]:
        # Latest sequence set -> best-ranked candidate.
        row = _q1(
            cur,
            """
            SELECT ssc.stop_node_ids, ssc.stop_prior_seqs
            FROM route_work.stop_sequence_candidate_sets scs
            JOIN route_work.stop_sequence_candidates ssc ON ssc.set_id = scs.set_id
            WHERE scs.route_id = %s
            ORDER BY scs.created_at DESC NULLS LAST, ssc.rank ASC NULLS LAST, ssc.created_at ASC NULLS LAST
            LIMIT 1
            """,
            (rid_route,),
        )
        if not row:
            return (None, None)

        stop_node_ids: List[str] = []
        stop_prior_seqs: List[int] = []

        v1 = row.get("stop_node_ids")
        if isinstance(v1, (list, tuple)):
            stop_node_ids = [str(x) for x in v1 if x]
        elif isinstance(v1, str):
            s = v1.strip()
            if s.startswith("{") and s.endswith("}"):
                stop_node_ids = [p.strip().strip('"') for p in s[1:-1].split(",") if p.strip()]

        v2 = row.get("stop_prior_seqs")
        if isinstance(v2, (list, tuple)):
            for x in v2:
                try:
                    stop_prior_seqs.append(int(x))
                except Exception:
                    continue
        elif isinstance(v2, str):
            s = v2.strip()
            if s.startswith("{") and s.endswith("}"):
                for p in s[1:-1].split(","):
                    p = p.strip()
                    if not p:
                        continue
                    try:
                        stop_prior_seqs.append(int(p))
                    except Exception:
                        continue

        # A) use stop_prior_seqs first (direct endpoint semantics)
        if len(stop_prior_seqs) >= 2:
            first_seq = int(stop_prior_seqs[0])
            last_seq = int(stop_prior_seqs[-1])
            row_a = _q1(
                cur,
                """
                SELECT
                  MAX(CASE WHEN seq = %s THEN COALESCE(osm_ref::bigint, osm_node_id::bigint) END) AS first_osm,
                  MAX(CASE WHEN seq = %s THEN COALESCE(osm_ref::bigint, osm_node_id::bigint) END) AS last_osm
                FROM route_work.relation_stop_prior
                WHERE route_id = %s
                """,
                (first_seq, last_seq, rid_route),
            )
            if row_a and row_a.get("first_osm") and row_a.get("last_osm"):
                return (int(row_a["first_osm"]), int(row_a["last_osm"]))

        # B) fallback: map first/last stop_node_ids to matched_stop_node_id in prior table.
        if len(stop_node_ids) >= 2:
            first_stop = stop_node_ids[0]
            last_stop = stop_node_ids[-1]
            row_b = _q1(
                cur,
                """
                SELECT
                  MAX(CASE WHEN matched_stop_node_id::text = %s THEN COALESCE(osm_ref::bigint, osm_node_id::bigint) END) AS first_osm,
                  MAX(CASE WHEN matched_stop_node_id::text = %s THEN COALESCE(osm_ref::bigint, osm_node_id::bigint) END) AS last_osm
                FROM route_work.relation_stop_prior
                WHERE route_id = %s
                """,
                (first_stop, last_stop, rid_route),
            )
            if row_b and row_b.get("first_osm") and row_b.get("last_osm"):
                return (int(row_b["first_osm"]), int(row_b["last_osm"]))

        return (None, None)

    def _relations_for_node(node_id: int) -> Set[int]:
        q = build_candidate_relations_query([int(node_id)], route_regex=route_regex)
        resp = overpass_query(overpass_url, q, timeout_s=40, retries=2)
        out: Set[int] = set()
        for el in resp.get("elements", []):
            if el.get("type") != "relation":
                continue
            rid = el.get("id")
            if rid is None:
                continue
            try:
                out.add(int(rid))
            except Exception:
                continue
        return out

    # 1) Reuse chosen relation id from Phase 3.
    chosen_relation_id: Optional[int] = None
    with _conn(dsn_eff) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            chosen_relation_id = _load_phase3_chosen_relation_id(cur, route_id)

    if chosen_relation_id is not None:
        try:
            rel_full = overpass_query(
                overpass_url,
                build_relation_members_query(chosen_relation_id),
                timeout_s=70,
                retries=1,
            )
        except Exception:
            rel_full = {}
        cand = _candidate_from_relation(
            rid=chosen_relation_id,
            route_id_=route_id,
            source_type="osm_phase3_chosen_relation",
            overpass_raw=rel_full,
            overlap_count=0,
            overlap_ratio=0.0,
        )
        with _conn(dsn_eff) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                _persist_seed_if_possible(cur, cand)
        return [cand]

    # 2) Fallback: endpoint-node intersection (first & last stop).
    first_osm: Optional[int] = None
    last_osm: Optional[int] = None
    with _conn(dsn_eff) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            first_osm, last_osm = _load_endpoints_osm_node_ids(cur, route_id)

    if first_osm is None or last_osm is None:
        return []

    first_rels = _relations_for_node(first_osm)
    last_rels = _relations_for_node(last_osm)
    common = sorted(first_rels.intersection(last_rels))
    if not common:
        return []

    # deterministic pick: richest tags, then relation_id asc.
    scored: List[Tuple[int, int]] = []  # (tag_richness, relation_id)
    tag_cache: Dict[int, Dict[str, Any]] = {}
    for rid in common:
        try:
            rel_full = overpass_query(overpass_url, build_relation_members_query(rid), timeout_s=70, retries=1)
        except Exception:
            rel_full = {}
        tags = extract_relation_tags(rel_full, rid)
        tag_cache[rid] = {"raw": rel_full, "tags": tags}
        scored.append((tag_richness(tags), rid))

    scored.sort(key=lambda x: (-x[0], x[1]))
    chosen_fallback = scored[0][1]
    rel_payload = tag_cache.get(chosen_fallback, {}).get("raw") or {}
    cand = _candidate_from_relation(
        rid=chosen_fallback,
        route_id_=route_id,
        source_type="osm_endpoint_intersection_seed",
        overpass_raw=rel_payload,
        overlap_count=2,
        overlap_ratio=1.0,
    )
    with _conn(dsn_eff) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            _persist_seed_if_possible(cur, cand)
    return [cand]
