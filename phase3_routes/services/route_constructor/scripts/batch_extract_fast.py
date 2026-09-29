"""
Fast in-process batch route extractor for Valle de los Chillos.

Avoids subprocess overhead by running discovery + fetch directly in-process.
~5x faster than batch_extract_valle_chillos.py.

Usage:
  python scripts/batch_extract_fast.py
  python scripts/batch_extract_fast.py --max-attempts 20 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure project root is on path
REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PHASE3_ROOT = Path(__file__).resolve().parents[1]
if str(PHASE3_ROOT) not in sys.path:
    sys.path.insert(0, str(PHASE3_ROOT))

from dotenv import load_dotenv
load_dotenv()

import psycopg2
import psycopg2.extras
import requests

# Import from phase3 source
from src.evidence.overpass import search_route_relations, fetch_relation_overpass_json
from src.evidence.parse_relation import extract_stop_prior
from src.discover.operator_catalog import (
    canonicalize_operator_name,
    operator_match_score,
)


# ── Hardcoded bboxes ──
PLACE_BBOXES: Dict[str, Tuple[float, float, float, float]] = {
    "sangolqui":     (-0.36, -78.48, -0.28, -78.42),
    "san rafael":    (-0.32, -78.47, -0.27, -78.43),
    "fajardo":       (-0.34, -78.46, -0.30, -78.43),
    "el triangulo":  (-0.35, -78.47, -0.31, -78.44),
    "loreto":        (-0.36, -78.46, -0.32, -78.43),
    "rumiloma":      (-0.355, -78.465, -0.295, -78.405),
    "rumipamba":     (-0.34, -78.45, -0.29, -78.40),
    "tanipamba":     (-0.34, -78.45, -0.29, -78.40),
    "vallecito":     (-0.34, -78.45, -0.29, -78.40),
    "la moca":       (-0.36, -78.45, -0.31, -78.40),
    "el carmen":     (-0.34, -78.44, -0.30, -78.40),
    "la libertad":   (-0.34, -78.46, -0.29, -78.41),
    "amaguana":     (-0.41, -78.55, -0.35, -78.48),
    "conocoto":     (-0.32, -78.51, -0.27, -78.46),
    "tambillo":     (-0.43, -78.56, -0.38, -78.51),
    "pintag":       (-0.42, -78.42, -0.35, -78.36),
    "la marin":     (-0.24, -78.52, -0.21, -78.50),
    "cotogchoa":    (-0.39, -78.47, -0.33, -78.42),
    "san fernando":  (-0.34, -78.465, -0.29, -78.42),
    "san vicente":   (-0.35, -78.47, -0.29, -78.41),
    "el cabre":      (-0.35, -78.46, -0.31, -78.42),
    "inchalillo":    (-0.35, -78.45, -0.31, -78.41),
    "curipungo":     (-0.36, -78.44, -0.32, -78.40),
    "los tubos":     (-0.36, -78.44, -0.32, -78.40),
    "san antonio":   (-0.36, -78.44, -0.32, -78.40),
    "la salle":      (-0.32, -78.51, -0.28, -78.47),
    "quito":         (-0.30, -78.55, -0.15, -78.45),
    "chaupitena":    (-0.34, -78.48, -0.30, -78.44),
    "capelo":        (-0.31, -78.47, -0.28, -78.44),
    "valle oriental": (-0.40, -78.50, -0.26, -78.36),
    "san alfonso":   (-0.40, -78.42, -0.36, -78.38),
    "selva alegre":  (-0.34, -78.48, -0.30, -78.44),
    "iasa":          (-0.35, -78.47, -0.31, -78.43),
    "ruminahui":     (-0.40, -78.52, -0.26, -78.38),
    # DMQ / Quito urban anchors
    "terminal rio coca":    (-0.175, -78.49, -0.155, -78.47),
    "rio coca":             (-0.175, -78.49, -0.155, -78.47),
    "playon de la marin":   (-0.235, -78.515, -0.215, -78.505),
    "marin central":        (-0.235, -78.515, -0.215, -78.505),
    "terminal norte la y":  (-0.145, -78.50, -0.125, -78.48),
    "el recreo":            (-0.255, -78.535, -0.235, -78.515),
    "colon":                (-0.205, -78.51, -0.19, -78.49),
    "moran valverde":       (-0.275, -78.555, -0.255, -78.535),
    "guamani":              (-0.295, -78.565, -0.275, -78.545),
    "de las universidades": (-0.20, -78.51, -0.18, -78.49),
    "naciones unidas":      (-0.185, -78.50, -0.165, -78.48),
    "baca ortiz":           (-0.205, -78.51, -0.195, -78.49),
    "casa de la cultura":   (-0.215, -78.51, -0.20, -78.495),
    "sauces":               (-0.175, -78.49, -0.16, -78.475),
    "chimbacalle":          (-0.24, -78.525, -0.225, -78.51),
    "el comercio":          (-0.245, -78.535, -0.23, -78.52),
    "puente de guajalo":    (-0.27, -78.55, -0.25, -78.53),
    "capuli":               (-0.28, -78.56, -0.26, -78.54),
    "el labrador":          (-0.165, -78.49, -0.145, -78.47),
    "santo domingo":        (-0.225, -78.52, -0.215, -78.505),
    "avenida america":      (-0.20, -78.51, -0.16, -78.48),
    # Interparish places
    "calderon":             (-0.10, -78.44, -0.06, -78.40),
    "tumbaco":              (-0.22, -78.42, -0.18, -78.38),
    "cumbaya":              (-0.20, -78.45, -0.17, -78.42),
    "puembo":               (-0.18, -78.40, -0.14, -78.36),
    "yaruqui":              (-0.18, -78.37, -0.14, -78.33),
    "pomasqui":             (-0.08, -78.48, -0.04, -78.44),
    "san antonio de sample_region": (-0.03, -78.47, 0.01, -78.43),
    "oton de velez":        (-0.18, -78.37, -0.14, -78.33),
    # Quito wide zones
    "quito norte":          (-0.20, -78.52, -0.10, -78.46),
    "quito sur":            (-0.33, -78.57, -0.25, -78.50),
    "quito centro":         (-0.24, -78.53, -0.19, -78.49),
    "quito valles":         (-0.25, -78.45, -0.14, -78.33),
    # Mejía / Machachi
    "machachi":             (-0.53, -78.60, -0.49, -78.54),
    "machachi centro":      (-0.52, -78.58, -0.50, -78.55),
    "terminal terrestre de machachi": (-0.515, -78.575, -0.505, -78.555),
    "aloasi":               (-0.54, -78.62, -0.50, -78.58),
    "el chaupi":            (-0.58, -78.67, -0.54, -78.62),
    "aloag":                (-0.48, -78.60, -0.44, -78.56),
    "mejia":                (-0.58, -78.68, -0.43, -78.54),
    "el trebol":            (-0.22, -78.51, -0.20, -78.49),
    "terminal quitumbe":    (-0.295, -78.56, -0.275, -78.54),
    "panamericana sur":     (-0.55, -78.62, -0.30, -78.50),
}
FALLBACK_BBOX = (-0.58, -78.68, -0.02, -78.33)

CORRIDOR_BBOXES = {
    "ruminahui_wide":       (-0.40, -78.52, -0.26, -78.38),
    "valle_chillos_quito":  (-0.40, -78.55, -0.19, -78.38),
    "amaguana_corridor":    (-0.41, -78.55, -0.27, -78.46),
    "pintag_corridor":      (-0.42, -78.52, -0.21, -78.36),
    "rumipamba_rural":      (-0.36, -78.46, -0.28, -78.38),
    # DMQ corridors
    "trolebus_axis":        (-0.30, -78.56, -0.13, -78.48),
    "ecovia_axis":          (-0.30, -78.55, -0.16, -78.47),
    "central_norte":        (-0.24, -78.52, -0.10, -78.47),
    "sur_occidental":       (-0.33, -78.57, -0.22, -78.50),
    "east_valleys":         (-0.25, -78.46, -0.10, -78.33),
    "north_connectors":     (-0.12, -78.50, -0.02, -78.40),
    "dmq_full":             (-0.40, -78.58, -0.02, -78.33),
    # Mejía corridors
    "machachi_quito":       (-0.53, -78.60, -0.27, -78.50),
    "mejia_rural":          (-0.58, -78.68, -0.44, -78.54),
}


def _normalize(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


CORRIDOR_PHRASE_MAP: Dict[str, str] = {
    "ecovia": "ecovia_axis",
    "trolebus": "trolebus_axis",
    "metrobus q": "central_norte",
    "metrobus": "central_norte",
    "corredor central norte": "central_norte",
    "corredor sur occidental": "sur_occidental",
}


def _get_bbox(place: str) -> Tuple[float, float, float, float]:
    norm = _normalize(place)
    if norm in PLACE_BBOXES:
        return PLACE_BBOXES[norm]
    for key, bbox in PLACE_BBOXES.items():
        if key in norm or norm in key:
            return bbox
    if norm in CORRIDOR_BBOXES:
        return CORRIDOR_BBOXES[norm]
    # Corridor phrase mapping (e.g. "Ecovía" -> ecovia_axis)
    for phrase, corridor_key in CORRIDOR_PHRASE_MAP.items():
        if phrase in norm or norm in phrase:
            return CORRIDOR_BBOXES[corridor_key]
    for key, bbox in CORRIDOR_BBOXES.items():
        if key in norm or norm in key:
            return bbox
    return FALLBACK_BBOX


def _expand_bbox(bbox: Tuple[float, float, float, float], pct: float) -> Tuple[float, float, float, float]:
    s, w, n, e = bbox
    lat_span = n - s
    lon_span = e - w
    ds = lat_span * pct / 2
    dw = lon_span * pct / 2
    return (s - ds, w - dw, n + ds, e + dw)


def _score_candidate(cand: Dict[str, Any], *, operator_hint: str | None, route_hint: str | None) -> float:
    """Score a relation candidate for selection."""
    tags = cand.get("tags", {})
    score = 0.0

    # Prefer type=route over route_master
    rel_type = tags.get("type", "")
    if rel_type == "route":
        score += 1000
    elif rel_type == "route_master":
        score += 500

    # Member count bonus (more members = more detailed route)
    members = cand.get("members_count", 0)
    score += min(members, 100) * 2

    # Operator matching
    if operator_hint:
        op_key = canonicalize_operator_name(operator_hint)
        if op_key:
            score += operator_match_score(tags, [op_key])

    # Route hint matching (name/ref)
    if route_hint:
        hint_norm = _normalize(route_hint)
        ref = _normalize(tags.get("ref", ""))
        name = _normalize(tags.get("name", ""))
        if hint_norm and ref and hint_norm in ref:
            score += 100
        if hint_norm and name and hint_norm in name:
            score += 80
        # Token overlap
        hint_tokens = set(hint_norm.split())
        name_tokens = set(name.split()) | set(ref.split())
        overlap = len(hint_tokens & name_tokens)
        score += overlap * 20

    return score


def _create_route_job(conn, *, area_key: str, bbox: Dict, batch_id: str, notes: str) -> uuid.UUID:
    """Create a route_raw.route_jobs entry and return route_id."""
    route_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO route_raw.route_jobs (route_id, status, area_key, bbox, notes, created_by)
            VALUES (%s, 'new', %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (str(route_id), area_key, json.dumps(bbox), notes, os.getenv("USER", "batch_fast")))
    conn.commit()
    return route_id


def _store_candidates(conn, route_id: uuid.UUID, candidates: List[Dict], chosen_id: int):
    """Store candidates in relation_candidates and set chosen on route_jobs."""
    with conn.cursor() as cur:
        for cand in candidates:
            cur.execute("""
                INSERT INTO route_raw.relation_candidates
                    (candidate_id, route_id, osm_relation_id, rel_type, route_mode, ref, name, operator, tags)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (route_id, osm_relation_id) DO NOTHING
            """, (
                str(uuid.uuid4()),
                str(route_id),
                int(cand["id"]),
                cand.get("tags", {}).get("type", "route"),
                cand.get("tags", {}).get("route", "bus"),
                cand.get("tags", {}).get("ref"),
                cand.get("tags", {}).get("name"),
                cand.get("tags", {}).get("operator"),
                json.dumps(cand.get("tags", {})),
            ))

        # Set chosen relation
        cur.execute("""
            UPDATE route_raw.route_jobs
            SET chosen_osm_relation_id = %s, status = 'relation_fetched'
            WHERE route_id = %s
        """, (chosen_id, str(route_id)))

    conn.commit()


def _store_overpass_raw(conn, route_id: uuid.UUID, osm_relation_id: int, overpass_json: Dict):
    """Store raw Overpass response."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO route_raw.osm_relations_raw
                (route_id, osm_relation_id, overpass_json, fetched_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (route_id) DO UPDATE SET
                osm_relation_id = EXCLUDED.osm_relation_id,
                overpass_json = EXCLUDED.overpass_json,
                fetched_at = NOW()
        """, (str(route_id), osm_relation_id, json.dumps(overpass_json)))
    conn.commit()


def _store_extractor_review(conn, route_id: uuid.UUID, *, place: str, operator_hint: str | None,
                             route_hint: str | None, attempt_type: str, batch_id: str,
                             bbox: Dict, candidate_count: int, chosen_id: int | None,
                             confidence: float, source_name: str):
    """Store extractor review metadata on route_jobs."""
    review = {
        "batch_id": batch_id,
        "extractor": "batch_extract_fast",
        "source_name": source_name,
        "target_place": place,
        "cooperative_hint": operator_hint,
        "route_hint": route_hint,
        "attempt_type": attempt_type,
        "bbox": bbox,
        "candidate_count": candidate_count,
        "chosen_osm_relation_id": chosen_id,
        "selection_confidence": confidence,
        "attempt_history": [{
            "place": place,
            "cooperative_hint": operator_hint,
            "route_hint_raw": route_hint,
            "attempt_type": attempt_type,
        }],
    }
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE route_raw.route_jobs
            SET extractor_review = %s, extractor_source = %s
            WHERE route_id = %s
        """, (json.dumps(review), source_name, str(route_id)))
    conn.commit()


def run_fast_batch(
    catalog_path: str,
    *,
    max_attempts: int | None = None,
    dry_run: bool = False,
    fetch: bool = True,
    delay_s: float = 1.0,
    overpass_timeout_s: int = 60,
):
    catalog = json.loads(Path(catalog_path).read_text())
    source_name = Path(catalog_path).name

    # Build targets using the same logic as the original script
    sys.path.insert(0, str(REPO_ROOT))
    from datamind_console.phases.phase3_routes.client import Phase3Client
    attempts = Phase3Client._build_phase3_target_attempts(catalog)

    targets = []
    seen = set()
    for item in (attempts or []):
        item = dict(item or {})
        place = str(item.get("place") or "").strip()
        if not place:
            continue
        route_hint = str(item.get("route_hint") or "").strip() or None
        operator_hint = str(item.get("cooperative_hint") or "").strip() or None
        dedupe_key = json.dumps({
            "place": _normalize(place),
            "operator_hint": _normalize(operator_hint or ""),
            "route_hint": _normalize(route_hint or ""),
            "attempt_type": str(item.get("attempt_type") or ""),
        }, sort_keys=True)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        targets.append({
            "place": place,
            "operator_hint": operator_hint,
            "route_hint": route_hint,
            "attempt_type": str(item.get("attempt_type") or "catalog_attempt"),
            "bbox_hint": item.get("bbox_hint"),
        })

    # Check existing
    conn = psycopg2.connect(os.environ["DB_DSN"])
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT extractor_review FROM route_raw.route_jobs
            WHERE extractor_source = %s AND extractor_review IS NOT NULL
        """, (source_name,))
        existing_keys = set()
        for row in cur.fetchall():
            review = row.get("extractor_review") or {}
            for ah in (review.get("attempt_history") or [review]):
                existing_keys.add(json.dumps({
                    "place": _normalize(str(ah.get("place") or ah.get("target_place") or "")),
                    "operator_hint": _normalize(str(ah.get("cooperative_hint") or "")),
                    "route_hint": _normalize(str(ah.get("route_hint_raw") or ah.get("route_hint") or "")),
                    "attempt_type": str(ah.get("attempt_type") or ""),
                }, sort_keys=True))

    skipped = 0
    filtered = []
    for t in targets:
        key = json.dumps({
            "place": _normalize(t["place"]),
            "operator_hint": _normalize(t.get("operator_hint") or ""),
            "route_hint": _normalize(t.get("route_hint") or ""),
            "attempt_type": t.get("attempt_type", ""),
        }, sort_keys=True)
        if key in existing_keys:
            skipped += 1
            continue
        filtered.append(t)
    targets = filtered

    if max_attempts is not None:
        targets = targets[:max_attempts]

    batch_id = f"fast_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    print(f"Batch: {batch_id}")
    print(f"Total targets: {len(targets)}")
    if skipped:
        print(f"Skipped existing: {skipped}")

    if dry_run:
        for i, t in enumerate(targets, 1):
            bbox = _get_bbox(t["place"])
            print(f"  [{i:3d}] {t['place']:<20s} op={str(t.get('operator_hint') or '-'):<25s} "
                  f"hint={str(t.get('route_hint') or '-'):<40s}")
        return

    successes = []
    failures = []
    relation_ids_seen = set()

    for idx, target in enumerate(targets, 1):
        place = target["place"]
        operator_hint = target.get("operator_hint")
        route_hint = target.get("route_hint")
        attempt_type = target.get("attempt_type", "unknown")

        # Resolve bbox
        bbox_hint = target.get("bbox_hint")
        if isinstance(bbox_hint, dict) and all(k in bbox_hint for k in ("south", "west", "north", "east")):
            bbox = (float(bbox_hint["south"]), float(bbox_hint["west"]),
                    float(bbox_hint["north"]), float(bbox_hint["east"]))
        else:
            bbox = _get_bbox(place)

        # Try discovery with increasing bbox expansion
        candidates = []
        for expand_pct in (0.0, 0.20, 0.40):
            expanded = _expand_bbox(bbox, expand_pct)
            try:
                candidates = search_route_relations(
                    expanded,
                    operator_contains=operator_hint,
                    name_contains=route_hint,
                    query_strategy="bbox_first_broad",
                    timeout_s=overpass_timeout_s,
                    limit=50,
                )
                if candidates:
                    bbox = expanded
                    break
            except Exception as exc:
                err = str(exc)
                if "429" in err or "504" in err:
                    print(f"  [{idx:3d}] Rate limited, waiting 10s...")
                    time.sleep(10)
                    continue
                break

        if not candidates:
            failures.append({"place": place, "reason": "no_candidates"})
            print(f"  [{idx:3d}] FAIL {place:<20s} - no candidates found")
            time.sleep(delay_s)
            continue

        # Score and select best
        for c in candidates:
            c["_score"] = _score_candidate(c, operator_hint=operator_hint, route_hint=route_hint)
        candidates.sort(key=lambda c: c["_score"], reverse=True)
        chosen = candidates[0]
        chosen_id = int(chosen["id"])
        confidence = min(1.0, chosen["_score"] / 1500)
        relation_ids_seen.add(chosen_id)

        bbox_dict = {"south": bbox[0], "west": bbox[1], "north": bbox[2], "east": bbox[3]}

        # Create route job
        route_id = _create_route_job(
            conn,
            area_key=_normalize(place),
            bbox=bbox_dict,
            batch_id=batch_id,
            notes=f"fast batch | {attempt_type} | {batch_id}",
        )

        # Store candidates
        _store_candidates(conn, route_id, candidates, chosen_id)

        # Store extractor review
        _store_extractor_review(
            conn, route_id,
            place=place,
            operator_hint=operator_hint,
            route_hint=route_hint,
            attempt_type=attempt_type,
            batch_id=batch_id,
            bbox=bbox_dict,
            candidate_count=len(candidates),
            chosen_id=chosen_id,
            confidence=confidence,
            source_name=source_name,
        )

        # Fetch relation if enabled
        fetch_ok = False
        if fetch:
            try:
                overpass_json = fetch_relation_overpass_json(chosen_id, timeout_s=overpass_timeout_s)
                _store_overpass_raw(conn, route_id, chosen_id, overpass_json)
                fetch_ok = True
            except Exception as exc:
                print(f"  [{idx:3d}] WARN fetch failed rel={chosen_id}: {exc}")

        successes.append({
            "route_id": str(route_id),
            "place": place,
            "chosen_osm_relation_id": chosen_id,
            "candidate_count": len(candidates),
            "confidence": round(confidence, 2),
            "fetched": fetch_ok,
        })

        tags = chosen.get("tags", {})
        ref = tags.get("ref", "-")
        name = tags.get("name", "-")[:30]
        print(f"  [{idx:3d}] OK   {place:<20s} rel={chosen_id} ref={ref} "
              f"cands={len(candidates)} conf={confidence:.2f} fetched={fetch_ok}")

        time.sleep(delay_s)

    conn.close()

    # Summary
    print(f"\n{'='*60}")
    print(f"EXTRACTION COMPLETE")
    print(f"  Extracted:   {len(successes)}")
    print(f"  Failed:      {len(failures)}")
    print(f"  Unique rels: {len(relation_ids_seen)}")
    print(f"  Fetched:     {sum(1 for s in successes if s.get('fetched'))}")
    print(f"{'='*60}")

    return {
        "batch_id": batch_id,
        "extracted": len(successes),
        "failed": len(failures),
        "unique_relations": len(relation_ids_seen),
        "results": successes,
        "failures": failures,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Fast in-process batch route extraction")
    ap.add_argument("--catalog", default=str(REPO_ROOT / "phase3_routes/catalogs/valle_de_los_chillos_phase3_catalog.json"))
    ap.add_argument("--max-attempts", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-fetch", action="store_true")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--output", type=str, default=None)
    args = ap.parse_args()

    result = run_fast_batch(
        catalog_path=args.catalog,
        max_attempts=args.max_attempts,
        dry_run=args.dry_run,
        fetch=not args.skip_fetch,
        delay_s=args.delay,
        overpass_timeout_s=args.timeout,
    )

    if args.output and result:
        Path(args.output).write_text(json.dumps(result, indent=2, default=str))
        print(f"\nResults saved to: {args.output}")
