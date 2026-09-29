"""Detect patterns and hops in a running OTP that fell back to straight-line geometry.

Read-only, runs against the live OTP HTTP API. No rebuild, no DB writes.
Targets OTP 2.x (verified against 2.5.0 / serializationVersionId 148).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

import httpx
import polyline
from tqdm import tqdm


CACHE_DIR = Path(".cache/otp_audit")
CONNECT_TIMEOUT = 30.0
READ_TIMEOUT = 60.0
RETRY_ATTEMPTS = 3

FULL_FALLBACK_RATIO = 2.1
PARTIAL_RATIO_UPPER = 4.0
LOW_DENSITY_PER_100M = 1.5


# ---------- HTTP helpers ----------


class OtpError(RuntimeError):
    pass


def _encode_pid(pid: str) -> str:
    return urllib.parse.quote(pid, safe="")


def _request(client: httpx.Client, path: str, *, as_text: bool = False):
    last_exc: Exception | None = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            r = client.get(path)
            if r.status_code >= 500:
                raise OtpError(f"{path} → HTTP {r.status_code}")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.text if as_text else r.json()
        except (httpx.RequestError, OtpError) as exc:
            last_exc = exc
            if attempt < RETRY_ATTEMPTS - 1:
                time.sleep(0.5 * (2 ** attempt))
    raise OtpError(f"{path} failed after {RETRY_ATTEMPTS} attempts: {last_exc}")


def make_client(otp_url: str) -> httpx.Client:
    return httpx.Client(
        base_url=otp_url.rstrip("/"),
        timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=READ_TIMEOUT, pool=READ_TIMEOUT),
        headers={"Accept": "application/json"},
    )


# ---------- Pre-flight ----------


def preflight(client: httpx.Client, router: str) -> dict:
    """Verify OTP is reachable and the router exists. Returns {version, sver}."""
    info = None
    for path in ("/otp/serverInfo", "/otp"):
        try:
            info = _request(client, path)
        except OtpError:
            info = None
        if info:
            break
    if not info:
        sys.stderr.write(
            "FATAL: cannot reach OTP at the given URL. "
            "Try `lsof -i :8080` and confirm OTP is up.\n"
        )
        sys.exit(2)

    v = info.get("version") or {}
    version = v.get("version") if isinstance(v, dict) else str(v)
    sver = info.get("otpSerializationVersionId") or info.get("serializationVersionId")

    routers_doc = _request(client, "/otp/routers")
    routers = []
    if isinstance(routers_doc, dict):
        for r in routers_doc.get("routerInfo", []):
            routers.append(r.get("routerId"))
    if router not in routers:
        sys.stderr.write(
            f"FATAL: router '{router}' not found. "
            f"Available routers: {routers or '(none)'}. "
            f"Pass --router <name> with one of those.\n"
        )
        sys.exit(2)

    return {"version": version, "sver": sver}


# ---------- Math ----------


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def polyline_length_m(pts: list[tuple[float, float]]) -> float:
    if len(pts) < 2:
        return 0.0
    total = 0.0
    for i in range(1, len(pts)):
        total += haversine_m(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1])
    return total


def nearest_polyline_index(stop_lat: float, stop_lon: float, pts: list[tuple[float, float]]) -> int:
    best_i, best_d = 0, float("inf")
    for i, (la, lo) in enumerate(pts):
        d = haversine_m(stop_lat, stop_lon, la, lo)
        if d < best_d:
            best_d = d
            best_i = i
    return best_i


# ---------- Cache ----------


def cache_path(pid: str, semantic_hash: str) -> Path:
    key = f"{pid}|{semantic_hash}"
    h = hashlib.sha256(key.encode()).hexdigest()[:24]
    return CACHE_DIR / f"{h}.json"


def load_cached(pid: str, semantic_hash: str) -> dict | None:
    p = cache_path(pid, semantic_hash)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return None
    return None


def store_cached(pid: str, semantic_hash: str, payload: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path(pid, semantic_hash).write_text(json.dumps(payload))


# ---------- Data classes ----------


@dataclass
class PatternRow:
    route_id: str
    route_short_name: str
    pattern_id: str
    pattern_name: str
    n_stops: int
    n_pts: int
    ratio: float
    density_per_100m: float
    direct_distance_m: float
    total_distance_m: float
    tortuosity: float
    classification: str


@dataclass
class HopRow:
    pattern_id: str
    route_id: str
    hop_index: int
    from_stop_id: str
    to_stop_id: str
    hop_distance_m: float
    intermediate_points: int
    is_straight_line: bool


# ---------- Per-pattern audit ----------


def fetch_pattern_payload(client: httpx.Client, router: str, pid: str) -> dict | None:
    """Fetch (or load from cache) stops + decoded polyline for one pattern. Returns None on missing data."""
    pid_enc = _encode_pid(pid)
    sem = _request(client, f"/otp/routers/{router}/index/patterns/{pid_enc}/semanticHash", as_text=True)
    if sem is None:
        return None
    sem = sem.strip()
    cached = load_cached(pid, sem)
    if cached is not None:
        return cached

    stops = _request(client, f"/otp/routers/{router}/index/patterns/{pid_enc}/stops")
    geom = _request(client, f"/otp/routers/{router}/index/patterns/{pid_enc}/geometry")
    if stops is None or geom is None:
        return None

    points_str = (geom or {}).get("points") or ""
    decoded: list[tuple[float, float]] = polyline.decode(points_str) if points_str else []

    payload = {
        "semantic_hash": sem,
        "stops": [
            {"id": s.get("id", ""), "lat": s.get("lat"), "lon": s.get("lon")}
            for s in stops
            if s.get("lat") is not None and s.get("lon") is not None
        ],
        "polyline": [list(p) for p in decoded],
        "length_field": (geom or {}).get("length"),
    }
    store_cached(pid, sem, payload)
    return payload


def classify(ratio: float, density_per_100m: float) -> str:
    if ratio <= FULL_FALLBACK_RATIO:
        return "full_fallback"
    if ratio < PARTIAL_RATIO_UPPER or density_per_100m < LOW_DENSITY_PER_100M:
        return "partial_fallback"
    return "clean"


def build_pattern_row(pid: str, pat_desc: str, route_id: str, route_short: str, payload: dict) -> PatternRow:
    stops = payload["stops"]
    pts = [tuple(p) for p in payload["polyline"]]
    n_stops = len(stops)
    n_pts = len(pts)
    ratio = n_pts / max(n_stops, 1)
    total_distance_m = polyline_length_m(pts)
    direct_distance_m = (
        haversine_m(stops[0]["lat"], stops[0]["lon"], stops[-1]["lat"], stops[-1]["lon"])
        if n_stops >= 2
        else 0.0
    )
    density_per_100m = n_pts / (total_distance_m / 100.0) if total_distance_m > 0 else 0.0
    tortuosity = total_distance_m / max(direct_distance_m, 1.0)
    return PatternRow(
        route_id=route_id,
        route_short_name=route_short,
        pattern_id=pid,
        pattern_name=pat_desc,
        n_stops=n_stops,
        n_pts=n_pts,
        ratio=round(ratio, 4),
        density_per_100m=round(density_per_100m, 4),
        direct_distance_m=round(direct_distance_m, 2),
        total_distance_m=round(total_distance_m, 2),
        tortuosity=round(tortuosity, 4),
        classification=classify(ratio, density_per_100m),
    )


def build_hop_rows(pat_row: PatternRow, payload: dict) -> tuple[list[HopRow], list[str]]:
    stops = payload["stops"]
    pts = [tuple(p) for p in payload["polyline"]]
    if len(stops) < 2 or len(pts) < 2:
        return [], []
    indices = [nearest_polyline_index(s["lat"], s["lon"], pts) for s in stops]
    warnings: list[str] = []
    for i in range(1, len(indices)):
        if indices[i] < indices[i - 1]:
            warnings.append(
                f"WARN sequence-inversion in {pat_row.pattern_id}: "
                f"stop {i} idx={indices[i]} < stop {i-1} idx={indices[i-1]}"
            )
    rows: list[HopRow] = []
    for i in range(len(stops) - 1):
        gap = indices[i + 1] - indices[i] - 1
        intermediate = max(gap, 0)  # negative means inversion; reported as warning
        hop_d = haversine_m(stops[i]["lat"], stops[i]["lon"], stops[i + 1]["lat"], stops[i + 1]["lon"])
        rows.append(
            HopRow(
                pattern_id=pat_row.pattern_id,
                route_id=pat_row.route_id,
                hop_index=i,
                from_stop_id=stops[i]["id"],
                to_stop_id=stops[i + 1]["id"],
                hop_distance_m=round(hop_d, 2),
                intermediate_points=intermediate,
                is_straight_line=(intermediate == 0),
            )
        )
    return rows, warnings


# ---------- Orchestration ----------


def filter_routes(routes: list[dict], feed_id: str | None, route_id_set: set[str] | None) -> list[dict]:
    out = []
    for r in routes:
        rid = r.get("id", "")
        if feed_id and not rid.startswith(f"{feed_id}:"):
            continue
        if route_id_set:
            short = (r.get("shortName") or "")
            if rid not in route_id_set and short not in route_id_set:
                continue
        out.append(r)
    return out


def collect_patterns(client: httpx.Client, router: str, routes: list[dict]) -> list[tuple[dict, dict]]:
    """Returns list of (route, pattern) tuples."""
    pairs: list[tuple[dict, dict]] = []
    for r in tqdm(routes, desc="routes→patterns", unit="route"):
        rid = r["id"]
        try:
            pats = _request(client, f"/otp/routers/{router}/index/routes/{_encode_pid(rid)}/patterns") or []
        except OtpError as exc:
            sys.stderr.write(f"WARN failed to list patterns for {rid}: {exc}\n")
            continue
        for p in pats:
            pairs.append((r, p))
    return pairs


def audit_pattern(
    otp_url: str,
    router: str,
    route: dict,
    pattern: dict,
    want_hops: bool,
) -> tuple[PatternRow | None, list[HopRow], list[str]]:
    with make_client(otp_url) as client:
        try:
            payload = fetch_pattern_payload(client, router, pattern["id"])
        except OtpError as exc:
            sys.stderr.write(f"WARN failed pattern {pattern['id']}: {exc}\n")
            return None, [], []
        if payload is None or not payload.get("stops") or not payload.get("polyline"):
            return None, [], []
        pat_row = build_pattern_row(
            pid=pattern["id"],
            pat_desc=pattern.get("desc") or pattern.get("name") or "",
            route_id=route["id"],
            route_short=route.get("shortName") or "",
            payload=payload,
        )
        hop_rows: list[HopRow] = []
        warnings: list[str] = []
        if want_hops and pat_row.classification in ("partial_fallback", "full_fallback"):
            hop_rows, warnings = build_hop_rows(pat_row, payload)
        return pat_row, hop_rows, warnings


# ---------- CSV writers ----------


def write_pattern_csv(rows: list[PatternRow], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "route_id",
                "route_short_name",
                "pattern_id",
                "pattern_name",
                "n_stops",
                "n_pts",
                "ratio",
                "density_per_100m",
                "direct_distance_m",
                "total_distance_m",
                "tortuosity",
                "classification",
            ],
        )
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))


def write_hop_csv(rows: list[HopRow], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "pattern_id",
                "route_id",
                "hop_index",
                "from_stop_id",
                "to_stop_id",
                "hop_distance_m",
                "intermediate_points",
                "is_straight_line",
            ],
        )
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))


# ---------- Stdout summary ----------


def print_summary(
    info: dict,
    n_routes: int,
    pattern_rows: list[PatternRow],
    hop_rows: list[HopRow],
    top_n: int,
    want_hops: bool,
) -> None:
    n_pat = len(pattern_rows)
    counts = {"full_fallback": 0, "partial_fallback": 0, "clean": 0}
    for r in pattern_rows:
        counts[r.classification] = counts.get(r.classification, 0) + 1

    print(f"OTP version: {info.get('version')}  serializationVersionId: {info.get('sver')}")
    print(f"Scanned: {n_routes} routes, {n_pat} patterns\n")
    if n_pat == 0:
        print("  (no patterns matched filters)")
        return
    for k in ("full_fallback", "partial_fallback", "clean"):
        c = counts.get(k, 0)
        pct = 100.0 * c / n_pat if n_pat else 0.0
        print(f"  {k+':':<18}{c:>4} patterns ({pct:.1f}%)")
    print()

    worst = sorted(pattern_rows, key=lambda r: r.ratio)[:top_n]
    print(f"Worst {top_n} (by ratio asc):")
    print(
        f"  {'ROUTE_ID':<40} {'PATTERN_ID':<48} {'n_stops':>7} {'n_pts':>6} "
        f"{'ratio':>6} {'tortuosity':>10}  classification"
    )
    for r in worst:
        print(
            f"  {r.route_id:<40} {r.pattern_id:<48} {r.n_stops:>7} {r.n_pts:>6} "
            f"{r.ratio:>6.2f} {r.tortuosity:>10.3f}  {r.classification}"
        )

    if want_hops and hop_rows:
        total_hops = len(hop_rows)
        sl_hops = sum(1 for h in hop_rows if h.is_straight_line)
        pct = 100.0 * sl_hops / total_hops if total_hops else 0.0
        print(f"\nHop-level: {sl_hops:,} of {total_hops:,} hops are straight-line ({pct:.1f}%)")
        agg: dict[str, dict] = {}
        for h in hop_rows:
            a = agg.setdefault(h.route_id, {"total": 0, "sl": 0})
            a["total"] += 1
            if h.is_straight_line:
                a["sl"] += 1
        short_by_rid = {r.route_id: r.route_short_name for r in pattern_rows}
        ranked = sorted(agg.items(), key=lambda kv: kv[1]["sl"], reverse=True)[:10]
        print("Top 10 routes by straight-line hop count:")
        print(f"  {'ROUTE_ID':<40} {'short_name':<14} {'total_hops':>10} {'sl_hops':>8} {'pct':>6}")
        for rid, a in ranked:
            p = 100.0 * a["sl"] / a["total"] if a["total"] else 0.0
            print(
                f"  {rid:<40} {short_by_rid.get(rid,''):<14} {a['total']:>10} {a['sl']:>8} {p:>5.1f}%"
            )


# ---------- Main ----------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--otp-url", default="http://localhost:8080")
    p.add_argument("--router", default="default")
    p.add_argument("--feed-id", default=None)
    p.add_argument("--route-ids", default=None, help="CSV of route IDs or shortNames")
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    p.add_argument("--out", default=f"reports/otp_straight_line_audit_{ts}.csv")
    p.add_argument("--hop-detail", action="store_true")
    p.add_argument("--top", type=int, default=50)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--workers", type=int, default=8)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    route_id_set = set(s.strip() for s in args.route_ids.split(",") if s.strip()) if args.route_ids else None

    with make_client(args.otp_url) as client:
        info = preflight(client, args.router)
        print(f"OTP version: {info.get('version')}  serializationVersionId: {info.get('sver')}")
        all_routes = _request(client, f"/otp/routers/{args.router}/index/routes") or []
        routes = filter_routes(all_routes, args.feed_id, route_id_set)
        pairs = collect_patterns(client, args.router, routes)

    if args.limit is not None:
        pairs = pairs[: args.limit]

    pattern_rows: list[PatternRow] = []
    hop_rows: list[HopRow] = []
    warnings: list[str] = []

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(audit_pattern, args.otp_url, args.router, route, pat, args.hop_detail): (route, pat)
            for route, pat in pairs
        }
        for fut in tqdm(as_completed(futures), total=len(futures), desc="patterns", unit="pat"):
            try:
                pat_row, hops, warns = fut.result()
            except Exception as exc:
                route, pat = futures[fut]
                sys.stderr.write(f"WARN pattern {pat.get('id')} crashed: {exc}\n")
                continue
            if pat_row is not None:
                pattern_rows.append(pat_row)
            hop_rows.extend(hops)
            warnings.extend(warns)

    out_path = Path(args.out)
    write_pattern_csv(pattern_rows, out_path)
    print(f"\nWrote pattern CSV: {out_path} ({len(pattern_rows)} rows)")
    if args.hop_detail:
        hop_path = out_path.with_name(out_path.stem + "_hops.csv")
        write_hop_csv(hop_rows, hop_path)
        print(f"Wrote hop CSV:     {hop_path} ({len(hop_rows)} rows)")

    if warnings:
        print(f"\n{len(warnings)} sequence-inversion warning(s); first 5:")
        for w in warnings[:5]:
            print(f"  {w}")

    print()
    n_routes_with_patterns = len({r.route_id for r in pattern_rows})
    print_summary(info, n_routes_with_patterns, pattern_rows, hop_rows, args.top, args.hop_detail)

    return 0


if __name__ == "__main__":
    sys.exit(main())
