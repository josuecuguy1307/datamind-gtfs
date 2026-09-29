"""Validator for DR Type 2 (Stop Coverage Gap Filling) landmarks.

Reads ``workspace/dr_stop_coverage/parsed/batch_NN_<zone>.json`` produced
by the importer, scores every landmark using:

1. **Coord sanity** — decimals in range; landmark within an adaptive
   radius of the gap midpoint, ``max(500 m, gap_length / 2 + 300 m)``
   (the geometric ceiling for "inside the gap polyline" plus a small
   buffer for responder coord precision and midpoint-interpolation
   noise). Floor at 500 m so short urban gaps still get a useful
   tolerance window.
2. **Overpass confirmation** — self-hosted ``:12346``; POI / amenity /
   shop / named highway within 50 m → ``overpass_confidence = 0.9``.
   Nothing → proceed.
3. **Nominatim fallback** — public API, 1 req/sec hard. Specific named
   place with fuzzy match > 0.6 → ``0.6``; generic road/area → ``0.3``;
   timeout → ``0.2``.
4. **Combine** — ``responder = {high:0.8, medium:0.5, low:0.2}``;
   ``validation = max(overpass, nominatim)``; ``final = 0.5*r + 0.5*v``.
5. **Decision** — ``>= 0.6`` ACCEPT, ``0.4..0.6`` ACCEPT_UNCERTAIN,
   ``< 0.4`` REJECT.

Appends one JSONL row per landmark to
``workspace/dr_stop_coverage/_VALIDATION_LOG.jsonl`` and writes the
accepted landmarks into
``workspace/dr_stop_coverage/validated/batch_NN_<zone>.json``.
No writes to ``route_prod.*``.
"""
from __future__ import annotations

import argparse
import difflib
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import requests


ROOT = Path(__file__).resolve().parents[2]
PARSED_DIR = ROOT / "workspace" / "dr_stop_coverage" / "parsed"
VALIDATED_DIR = ROOT / "workspace" / "dr_stop_coverage" / "validated"
VALIDATION_LOG = ROOT / "workspace" / "dr_stop_coverage" / "_VALIDATION_LOG.jsonl"
DIAG_PATH = ROOT / "workspace" / "diagnostics" / "stop_coverage_diagnostic_full.jsonl"

OVERPASS_URL = os.environ.get(
    "OVERPASS_URL", "http://127.0.0.1:12346/api/interpreter"
)
NOMINATIM_URL = os.environ.get(
    "NOMINATIM_URL", "https://nominatim.openstreetmap.org/reverse"
)
USER_AGENT = "datamind-dr-stop-coverage-validator/1.0 (contact@example.com)"

RESPONDER_CONFIDENCE = {"high": 0.8, "medium": 0.5, "low": 0.2}

OVERPASS_RADIUS_M = 50
MIDPOINT_MAX_DISTANCE_M = 500     # floor for short gaps
MIDPOINT_HALF_GAP_BUFFER_M = 300  # buffer added to gap_length/2 for long gaps
OVERPASS_RATE_LIMIT_S = 0.2   # 5 req/sec
NOMINATIM_RATE_LIMIT_S = 1.1  # 1 req/sec hard


@dataclass
class ValidationResult:
    query_id: str
    route_code: str
    gap_number: int
    landmark_name: str
    approx_lat: float
    approx_lng: float
    responder_confidence_label: str
    responder_confidence_value: float
    coord_sanity_ok: bool
    coord_sanity_reason: Optional[str]
    overpass_confidence: float
    overpass_hit: Optional[dict[str, Any]]
    nominatim_confidence: float
    nominatim_match: Optional[dict[str, Any]]
    validation_confidence: float
    final_confidence: float
    decision: str


# ----------------------------------------------------------------------
# Utility
# ----------------------------------------------------------------------

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(h))


def _load_gap_index() -> dict[tuple[str, int], tuple[float, float, float]]:
    """Map (route_code, gap_idx) → (midpoint_lat, midpoint_lon, gap_m)
    from the diagnostic JSONL. Used to sanity-check proposed coords with
    a gap-length-aware threshold.
    """
    idx: dict[tuple[str, int], tuple[float, float, float]] = {}
    if not DIAG_PATH.exists():
        print(f"[validator] WARNING: diagnostic not found at {DIAG_PATH}; coord sanity will be skipped")
        return idx
    with DIAG_PATH.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            rc = rec["route_code"]
            for gap in rec.get("gaps", []):
                mc = gap.get("midpoint_coord") or [None, None]
                if mc[0] is None or mc[1] is None:
                    continue
                gap_m = float(gap.get("gap_m") or 0.0)
                idx[(rc, int(gap["idx"]))] = (float(mc[0]), float(mc[1]), gap_m)
    return idx


# ----------------------------------------------------------------------
# Overpass
# ----------------------------------------------------------------------

_last_overpass_call = 0.0


def _overpass_nearby(lat: float, lon: float, timeout_s: int = 20) -> list[dict[str, Any]]:
    """Return all nodes/ways with any tag within ``OVERPASS_RADIUS_M`` of
    (lat, lon). Rate-limited to 5 req/sec by sleeping before each call.
    """
    global _last_overpass_call
    delta = time.time() - _last_overpass_call
    if delta < OVERPASS_RATE_LIMIT_S:
        time.sleep(OVERPASS_RATE_LIMIT_S - delta)

    query = f"""[out:json][timeout:{timeout_s}];
(
  node(around:{OVERPASS_RADIUS_M},{lat},{lon})[name];
  node(around:{OVERPASS_RADIUS_M},{lat},{lon})[amenity];
  node(around:{OVERPASS_RADIUS_M},{lat},{lon})[shop];
  node(around:{OVERPASS_RADIUS_M},{lat},{lon})[highway];
  way(around:{OVERPASS_RADIUS_M},{lat},{lon})[name];
  way(around:{OVERPASS_RADIUS_M},{lat},{lon})[amenity];
  way(around:{OVERPASS_RADIUS_M},{lat},{lon})[shop];
  way(around:{OVERPASS_RADIUS_M},{lat},{lon})[highway];
);
out center tags;
"""
    try:
        r = requests.post(
            OVERPASS_URL,
            data={"data": query},
            headers={"User-Agent": USER_AGENT},
            timeout=timeout_s + 5,
        )
        _last_overpass_call = time.time()
        if r.status_code != 200:
            return []
        return r.json().get("elements", [])
    except (requests.RequestException, ValueError):
        _last_overpass_call = time.time()
        return []


def _score_overpass(elements: list[dict[str, Any]], name_hint: str) -> tuple[float, Optional[dict[str, Any]]]:
    if not elements:
        return 0.0, None
    # Prefer the best name-match among all hits; any hit at all already
    # means the coords point at something real.
    best_ratio = 0.0
    best_el: Optional[dict[str, Any]] = None
    for el in elements:
        tags = el.get("tags") or {}
        name = tags.get("name") or tags.get("name:es") or ""
        ratio = difflib.SequenceMatcher(a=name.lower(), b=name_hint.lower()).ratio() if name else 0.0
        if ratio > best_ratio:
            best_ratio = ratio
            best_el = el
    if best_el is None:
        best_el = elements[0]
    return 0.9, {
        "count": len(elements),
        "best_match_name": (best_el.get("tags") or {}).get("name"),
        "best_match_ratio": round(best_ratio, 3),
    }


# ----------------------------------------------------------------------
# Nominatim
# ----------------------------------------------------------------------

_last_nominatim_call = 0.0


def _nominatim_reverse(lat: float, lon: float, timeout_s: int = 10) -> Optional[dict[str, Any]]:
    global _last_nominatim_call
    delta = time.time() - _last_nominatim_call
    if delta < NOMINATIM_RATE_LIMIT_S:
        time.sleep(NOMINATIM_RATE_LIMIT_S - delta)
    params = {
        "format": "jsonv2",
        "lat": f"{lat:.6f}",
        "lon": f"{lon:.6f}",
        "zoom": "18",
        "addressdetails": "1",
    }
    url = NOMINATIM_URL + "?" + "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout_s)
        _last_nominatim_call = time.time()
        if r.status_code != 200:
            return None
        return r.json()
    except (requests.RequestException, ValueError):
        _last_nominatim_call = time.time()
        return None


def _score_nominatim(data: Optional[dict[str, Any]], name_hint: str) -> tuple[float, Optional[dict[str, Any]]]:
    if data is None:
        return 0.2, {"status": "timeout_or_error"}
    display_name = data.get("display_name") or ""
    category = data.get("category") or data.get("class") or ""
    place_type = data.get("type") or ""
    ratio = difflib.SequenceMatcher(
        a=display_name.lower(), b=name_hint.lower()
    ).ratio() if display_name else 0.0
    if category in {"amenity", "shop", "tourism", "leisure", "building", "public_transport"} and ratio > 0.6:
        return 0.6, {
            "display_name": display_name,
            "category": category,
            "type": place_type,
            "ratio": round(ratio, 3),
        }
    # Fallback: generic road / area / administrative boundary.
    if category in {"highway", "boundary", "place"} or place_type in {"residential", "suburb", "neighbourhood"}:
        return 0.3, {
            "display_name": display_name,
            "category": category,
            "type": place_type,
            "ratio": round(ratio, 3),
        }
    return 0.3, {
        "display_name": display_name,
        "category": category,
        "type": place_type,
        "ratio": round(ratio, 3),
    }


# ----------------------------------------------------------------------
# Per-landmark validation
# ----------------------------------------------------------------------

def _midpoint_threshold_m(gap_length_m: Optional[float]) -> float:
    """Adaptive sanity-radius. Floor 500 m for short gaps; for longer
    gaps allow up to gap_length/2 + 300 m (geometric upper bound on
    "inside the polyline" plus buffer for responder precision).
    """
    if gap_length_m is None or gap_length_m <= 0:
        return float(MIDPOINT_MAX_DISTANCE_M)
    return max(
        float(MIDPOINT_MAX_DISTANCE_M),
        gap_length_m / 2.0 + MIDPOINT_HALF_GAP_BUFFER_M,
    )


def _coord_sanity(
    lat: float,
    lon: float,
    midpoint: Optional[tuple[float, float]],
    gap_length_m: Optional[float] = None,
) -> tuple[bool, Optional[str]]:
    if not (-4.5 <= lat <= 2.0) or not (-82.0 <= lon <= -75.0):
        return False, "coords_outside_ecuador_bbox"
    if midpoint is None:
        return True, None
    d = _haversine_m(lat, lon, midpoint[0], midpoint[1])
    threshold = _midpoint_threshold_m(gap_length_m)
    if d > threshold:
        return False, (
            f"coord_too_far_from_midpoint_{int(d)}m_threshold_{int(threshold)}m"
        )
    return True, None


def _decide(final: float) -> str:
    if final >= 0.6:
        return "ACCEPT"
    if final >= 0.4:
        return "ACCEPT_UNCERTAIN"
    return "REJECT"


def validate_landmark(
    *,
    query_id: str,
    route_code: str,
    gap_number: int,
    name: str,
    lat: float,
    lon: float,
    confidence_label: str,
    midpoint: Optional[tuple[float, float]],
    gap_length_m: Optional[float] = None,
    skip_network: bool = False,
) -> ValidationResult:
    responder_val = RESPONDER_CONFIDENCE.get(confidence_label, 0.5)
    sanity_ok, sanity_reason = _coord_sanity(lat, lon, midpoint, gap_length_m)

    overpass_conf = 0.0
    overpass_hit: Optional[dict[str, Any]] = None
    nominatim_conf = 0.0
    nominatim_match: Optional[dict[str, Any]] = None

    if sanity_ok and not skip_network:
        els = _overpass_nearby(lat, lon)
        overpass_conf, overpass_hit = _score_overpass(els, name)
        if overpass_conf == 0.0:
            data = _nominatim_reverse(lat, lon)
            nominatim_conf, nominatim_match = _score_nominatim(data, name)

    validation_conf = max(overpass_conf, nominatim_conf)
    if not sanity_ok:
        final = 0.0
        decision = "REJECT"
    else:
        final = 0.5 * responder_val + 0.5 * validation_conf
        decision = _decide(final)
    return ValidationResult(
        query_id=query_id,
        route_code=route_code,
        gap_number=gap_number,
        landmark_name=name,
        approx_lat=lat,
        approx_lng=lon,
        responder_confidence_label=confidence_label,
        responder_confidence_value=responder_val,
        coord_sanity_ok=sanity_ok,
        coord_sanity_reason=sanity_reason,
        overpass_confidence=overpass_conf,
        overpass_hit=overpass_hit,
        nominatim_confidence=nominatim_conf,
        nominatim_match=nominatim_match,
        validation_confidence=validation_conf,
        final_confidence=round(final, 3),
        decision=decision,
    )


# ----------------------------------------------------------------------
# Batch validation
# ----------------------------------------------------------------------

def _append_log_row(row: dict[str, Any]) -> None:
    VALIDATION_LOG.parent.mkdir(parents=True, exist_ok=True)
    with VALIDATION_LOG.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def validate_batch(parsed_path: Path, *, skip_network: bool = False) -> dict[str, Any]:
    with parsed_path.open() as fh:
        parsed = json.load(fh)
    gap_idx = _load_gap_index()

    accepted: list[dict[str, Any]] = []
    uncertain: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    per_query: list[dict[str, Any]] = []
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for q in parsed.get("queries", []):
        if not q.get("found"):
            continue
        results_for_query: list[ValidationResult] = []
        gap_info = gap_idx.get((q["route_code"], q["gap_number"] - 1))
        if gap_info is None:
            midpoint = None
            gap_length_m = None
        else:
            midpoint = (gap_info[0], gap_info[1])
            gap_length_m = gap_info[2]
        for lm in q.get("landmarks", []):
            res = validate_landmark(
                query_id=q["query_id"],
                route_code=q["route_code"],
                gap_number=q["gap_number"],
                name=lm["name"],
                lat=lm["approx_lat"],
                lon=lm["approx_lng"],
                confidence_label=lm.get("confidence") or "medium",
                midpoint=midpoint,
                gap_length_m=gap_length_m,
                skip_network=skip_network,
            )
            results_for_query.append(res)
            log_row = {
                "ts": ts,
                "batch_file": parsed_path.name,
                "query_id": res.query_id,
                "route_code": res.route_code,
                "gap_number": res.gap_number,
                "landmark_name": res.landmark_name,
                "approx_lat": res.approx_lat,
                "approx_lng": res.approx_lng,
                "decision": res.decision,
                "final_confidence": res.final_confidence,
                "responder_confidence_label": res.responder_confidence_label,
                "overpass_confidence": res.overpass_confidence,
                "overpass_hit": res.overpass_hit,
                "nominatim_confidence": res.nominatim_confidence,
                "nominatim_match": res.nominatim_match,
                "coord_sanity_ok": res.coord_sanity_ok,
                "coord_sanity_reason": res.coord_sanity_reason,
            }
            _append_log_row(log_row)

            bucket_row = {
                "query_id": res.query_id,
                "route_code": res.route_code,
                "gap_number": res.gap_number,
                "name": res.landmark_name,
                "approx_lat": res.approx_lat,
                "approx_lng": res.approx_lng,
                "final_confidence": res.final_confidence,
                "decision": res.decision,
            }
            if res.decision == "ACCEPT":
                accepted.append(bucket_row)
            elif res.decision == "ACCEPT_UNCERTAIN":
                uncertain.append(bucket_row)
            else:
                rejected.append(bucket_row)
        per_query.append(
            {
                "query_id": q["query_id"],
                "route_code": q["route_code"],
                "gap_number": q["gap_number"],
                "results": [r.__dict__ for r in results_for_query],
            }
        )

    VALIDATED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = VALIDATED_DIR / parsed_path.name
    summary = {
        "schema": "dr_stop_coverage_validator/v1",
        "batch_file": parsed_path.name,
        "validated_at": ts,
        "skip_network": skip_network,
        "counts": {
            "accepted": len(accepted),
            "accept_uncertain": len(uncertain),
            "rejected": len(rejected),
        },
        "accepted": accepted,
        "accept_uncertain": uncertain,
        "rejected": rejected,
    }
    out_path.write_text(json.dumps(summary, indent=2))
    return summary


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="DR Type 2 validator")
    ap.add_argument(
        "paths",
        nargs="*",
        help="Parsed JSON files. Defaults to all batch_*.json in parsed/.",
    )
    ap.add_argument(
        "--skip-network",
        action="store_true",
        help="Skip Overpass + Nominatim (sanity-only). Useful for self-tests.",
    )
    args = ap.parse_args()

    if args.paths:
        targets = [Path(p) for p in args.paths]
    else:
        PARSED_DIR.mkdir(parents=True, exist_ok=True)
        targets = sorted(PARSED_DIR.glob("batch_*.json"))

    if not targets:
        print(f"[validator] no parsed files found in {PARSED_DIR}")
        return 0

    total_counts = {"accepted": 0, "accept_uncertain": 0, "rejected": 0}
    for path in targets:
        summary = validate_batch(path, skip_network=args.skip_network)
        c = summary["counts"]
        for k in total_counts:
            total_counts[k] += c[k]
        print(
            f"[validator] {path.name}: accepted={c['accepted']} "
            f"uncertain={c['accept_uncertain']} rejected={c['rejected']}"
        )
    print(f"[validator] TOTAL: {total_counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
