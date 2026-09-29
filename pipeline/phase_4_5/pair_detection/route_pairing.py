from __future__ import annotations

import math
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence, Tuple

LonLat = Tuple[float, float]

_DIRECTION_WORD_GROUPS: Dict[str, set[str]] = {
    "north": {"north", "northbound", "n", "nb"},
    "south": {"south", "southbound", "s", "sb"},
    "east": {"east", "eastbound", "e", "eb"},
    "west": {"west", "westbound", "w", "wb"},
    "inbound": {"inbound", "in", "ib"},
    "outbound": {"outbound", "out", "ob"},
    "clockwise": {"clockwise", "cw"},
    "counterclockwise": {"counterclockwise", "ccw", "anticlockwise"},
}

_OPPOSITE_GROUPS: Dict[str, str] = {
    "north": "south",
    "south": "north",
    "east": "west",
    "west": "east",
    "inbound": "outbound",
    "outbound": "inbound",
    "clockwise": "counterclockwise",
    "counterclockwise": "clockwise",
}


def clip01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def normalize_text(text: Any) -> str:
    raw = str(text or "").strip().lower()
    if not raw:
        return ""
    raw = unicodedata.normalize("NFKD", raw)
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = re.sub(r"[^a-z0-9]+", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def tokenize(text: Any) -> List[str]:
    norm = normalize_text(text)
    return [tok for tok in norm.split(" ") if tok]


def token_jaccard(a: Any, b: Any) -> Optional[float]:
    ta = set(tokenize(a))
    tb = set(tokenize(b))
    if not ta or not tb:
        return None
    inter = len(ta.intersection(tb))
    union = len(ta.union(tb))
    if union <= 0:
        return None
    return float(inter) / float(union)


def text_similarity(a: Any, b: Any) -> Optional[float]:
    aa = normalize_text(a)
    bb = normalize_text(b)
    if not aa or not bb:
        return None
    seq = SequenceMatcher(None, aa, bb).ratio()
    jac = token_jaccard(aa, bb)
    if jac is None:
        return clip01(seq)
    return clip01(0.65 * float(seq) + 0.35 * float(jac))


from hades.geometry.canonical import bearing as _canonical_bearing
from hades.geometry.canonical import haversine_m as _canonical_haversine_m


def haversine_m(a: LonLat, b: LonLat) -> float:
    """Great-circle distance for ``LonLat`` (lon-first) tuples used in this module."""
    return _canonical_haversine_m(float(a[1]), float(a[0]), float(b[1]), float(b[0]))


def parse_linestring_wkt(wkt: Any) -> List[LonLat]:
    text = str(wkt or "").strip()
    if not text:
        return []
    up = text.upper()
    if up.startswith("LINESTRING"):
        inner = text[text.find("(") + 1 : text.rfind(")")]
        return _parse_wkt_coord_list(inner)
    if up.startswith("MULTILINESTRING"):
        inner = text[text.find("(") + 1 : text.rfind(")")]
        chunks = inner.replace("),(", ")|(").split("|")
        out: List[LonLat] = []
        for chunk in chunks:
            c = chunk.strip().strip("(").strip(")")
            if not c:
                continue
            out.extend(_parse_wkt_coord_list(c))
        return out
    return []


def _parse_wkt_coord_list(inner: str) -> List[LonLat]:
    pts: List[LonLat] = []
    for part in inner.split(","):
        p = part.strip()
        if not p:
            continue
        xy = p.split()
        if len(xy) < 2:
            continue
        try:
            lon = float(xy[0])
            lat = float(xy[1])
        except Exception:
            continue
        pts.append((lon, lat))
    return pts


def polyline_length_m(points: Sequence[LonLat]) -> float:
    if len(points) < 2:
        return 0.0
    total = 0.0
    for idx in range(1, len(points)):
        total += haversine_m(points[idx - 1], points[idx])
    return float(total)


def downsample_points(points: Sequence[LonLat], *, max_points: int = 80) -> List[LonLat]:
    pts = list(points or [])
    if len(pts) <= max(2, int(max_points)):
        return pts
    step = max(1, int(math.ceil(len(pts) / float(max_points))))
    out = [pts[i] for i in range(0, len(pts), step)]
    if out[-1] != pts[-1]:
        out.append(pts[-1])
    return out


def _xy_from_lonlat(point: LonLat, *, lat0_deg: float) -> Tuple[float, float]:
    lon, lat = point
    lat0 = math.radians(float(lat0_deg))
    x = math.radians(float(lon)) * 6371000.0 * math.cos(lat0)
    y = math.radians(float(lat)) * 6371000.0
    return (x, y)


def _segment_projection(point: LonLat, a: LonLat, b: LonLat, *, lat0: float) -> Tuple[float, float, LonLat]:
    px, py = _xy_from_lonlat(point, lat0_deg=lat0)
    ax, ay = _xy_from_lonlat(a, lat0_deg=lat0)
    bx, by = _xy_from_lonlat(b, lat0_deg=lat0)
    vx = bx - ax
    vy = by - ay
    wx = px - ax
    wy = py - ay
    vv = vx * vx + vy * vy
    if vv <= 1e-12:
        dist = math.hypot(px - ax, py - ay)
        return (0.0, float(dist), a)
    t = (wx * vx + wy * vy) / vv
    t_clamped = max(0.0, min(1.0, t))
    cx = ax + t_clamped * vx
    cy = ay + t_clamped * vy
    dist = math.hypot(px - cx, py - cy)

    lon_a, lat_a = a
    lon_b, lat_b = b
    proj = (
        float(lon_a + (lon_b - lon_a) * t_clamped),
        float(lat_a + (lat_b - lat_a) * t_clamped),
    )
    return (float(t_clamped), float(dist), proj)


def nearest_distance_to_polyline_m(point: LonLat, polyline: Sequence[LonLat]) -> Optional[float]:
    line = list(polyline or [])
    if not line:
        return None
    if len(line) == 1:
        return haversine_m(point, line[0])
    lat0 = (point[1] + line[0][1] + line[-1][1]) / 3.0
    best = float("inf")
    for i in range(1, len(line)):
        _, dist, _ = _segment_projection(point, line[i - 1], line[i], lat0=lat0)
        if dist < best:
            best = dist
    return float(best)


def project_point_fraction_on_polyline(point: LonLat, polyline: Sequence[LonLat]) -> Optional[Tuple[float, float]]:
    line = list(polyline or [])
    if len(line) < 2:
        return None
    total_len = polyline_length_m(line)
    if total_len <= 1e-9:
        return None

    lat0 = (point[1] + line[0][1] + line[-1][1]) / 3.0
    best_dist = float("inf")
    best_frac = 0.0
    walked = 0.0

    for i in range(1, len(line)):
        seg_len = haversine_m(line[i - 1], line[i])
        if seg_len <= 1e-9:
            continue
        t, dist, _ = _segment_projection(point, line[i - 1], line[i], lat0=lat0)
        if dist < best_dist:
            best_dist = dist
            best_frac = (walked + t * seg_len) / total_len
        walked += seg_len

    return (clip01(float(best_frac)), float(best_dist))


def score_from_distance(dist_m: Optional[float], *, good_m: float, bad_m: float) -> Optional[float]:
    if dist_m is None:
        return None
    d = float(dist_m)
    g = max(1e-9, float(good_m))
    b = max(g + 1e-9, float(bad_m))
    if d <= g:
        return 1.0
    if d >= b:
        return 0.0
    return clip01(1.0 - ((d - g) / (b - g)))


def pair_points_by_distance(
    points_a: Sequence[LonLat],
    points_b: Sequence[LonLat],
    *,
    max_distance_m: float,
) -> List[Tuple[int, int, float]]:
    a = list(points_a or [])
    b = list(points_b or [])
    if not a or not b:
        return []
    candidates: List[Tuple[float, int, int]] = []
    threshold = float(max_distance_m)
    for ia, pa in enumerate(a):
        for ib, pb in enumerate(b):
            d = haversine_m(pa, pb)
            if d <= threshold:
                candidates.append((float(d), ia, ib))
    if not candidates:
        return []

    candidates.sort(key=lambda x: (x[0], x[1], x[2]))
    used_a: set[int] = set()
    used_b: set[int] = set()
    out: List[Tuple[int, int, float]] = []
    for dist, ia, ib in candidates:
        if ia in used_a or ib in used_b:
            continue
        used_a.add(ia)
        used_b.add(ib)
        out.append((ia, ib, float(dist)))
    out.sort(key=lambda x: x[0])
    return out


def _lcs_len(seq_a: Sequence[str], seq_b: Sequence[str]) -> int:
    a = list(seq_a or [])
    b = list(seq_b or [])
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
            else:
                cur[j] = max(prev[j], cur[j - 1])
        prev = cur
    return int(prev[-1])


def reverse_sequence_similarity(seq_a: Sequence[str], seq_b: Sequence[str]) -> Optional[float]:
    a = [str(x) for x in (seq_a or []) if str(x)]
    b = [str(x) for x in (seq_b or []) if str(x)]
    if not a or not b:
        return None
    rev_b = list(reversed(b))
    lcs = _lcs_len(a, rev_b)
    denom = float(max(1, min(len(a), len(b))))
    return clip01(float(lcs) / denom)


def _lnis_len(values: Sequence[float]) -> int:
    vals = [float(v) for v in (values or [])]
    if not vals:
        return 0
    tails: List[float] = []
    for v in vals:
        lo = 0
        hi = len(tails)
        while lo < hi:
            mid = (lo + hi) // 2
            if tails[mid] <= v:
                lo = mid + 1
            else:
                hi = mid
        if lo == len(tails):
            tails.append(v)
        else:
            tails[lo] = v
    return len(tails)


def reverse_order_score(pairs: Sequence[Tuple[int, int, float]], *, len_b: int) -> Optional[float]:
    pp = sorted((int(a), int(b), float(d)) for a, b, d in (pairs or []))
    if len(pp) < 2 or int(len_b) <= 1:
        return None
    projected = [float((int(len_b) - 1) - b) for _, b, _ in pp]
    lnis = _lnis_len(projected)
    return clip01(float(lnis) / float(len(pp)))


def corridor_overlap_score(
    line_a: Sequence[LonLat],
    line_b: Sequence[LonLat],
    *,
    threshold_m: float = 100.0,
) -> Optional[float]:
    a = downsample_points(line_a, max_points=80)
    b = downsample_points(line_b, max_points=80)
    if len(a) < 2 or len(b) < 2:
        return None

    def _coverage(src: List[LonLat], dst: List[LonLat]) -> float:
        hits = 0
        for p in src:
            d = nearest_distance_to_polyline_m(p, dst)
            if d is not None and d <= float(threshold_m):
                hits += 1
        return float(hits) / float(max(1, len(src)))

    cov_ab = _coverage(a, b)
    cov_ba = _coverage(b, a)
    return clip01((cov_ab + cov_ba) / 2.0)


def path_similarity_score(
    line_a: Sequence[LonLat],
    line_b: Sequence[LonLat],
    *,
    good_m: float = 80.0,
    bad_m: float = 500.0,
) -> Optional[float]:
    a = downsample_points(line_a, max_points=90)
    b = downsample_points(line_b, max_points=90)
    if len(a) < 2 or len(b) < 2:
        return None

    def _avg_dist(src: List[LonLat], dst: List[LonLat]) -> Optional[float]:
        dists: List[float] = []
        for p in src:
            d = nearest_distance_to_polyline_m(p, dst)
            if d is not None:
                dists.append(float(d))
        if not dists:
            return None
        return float(sum(dists) / float(len(dists)))

    d_ab = _avg_dist(a, b)
    d_ba = _avg_dist(b, a)
    if d_ab is None and d_ba is None:
        return None
    vals = [x for x in [d_ab, d_ba] if x is not None]
    avg = float(sum(vals) / float(len(vals)))
    return score_from_distance(avg, good_m=good_m, bad_m=bad_m)


def _bearing_deg(a: LonLat, b: LonLat) -> float:
    """Initial bearing for ``LonLat`` (lon-first) tuples used in this module."""
    return _canonical_bearing(float(a[1]), float(a[0]), float(b[1]), float(b[0]))


def shape_direction_opposition_score(line_a: Sequence[LonLat], line_b: Sequence[LonLat]) -> Optional[float]:
    a = list(line_a or [])
    b = list(line_b or [])
    if len(a) < 2 or len(b) < 2:
        return None
    ba = _bearing_deg(a[0], a[-1])
    bb = _bearing_deg(b[0], b[-1])
    diff = abs(ba - bb)
    diff = min(diff, 360.0 - diff)
    to_opposite = abs(180.0 - diff)
    return clip01(1.0 - min(180.0, to_opposite) / 180.0)


def endpoint_swap_score(
    a_start: Optional[LonLat],
    a_end: Optional[LonLat],
    b_start: Optional[LonLat],
    b_end: Optional[LonLat],
) -> Optional[float]:
    if not (a_start and a_end and b_start and b_end):
        return None
    cross = (haversine_m(a_start, b_end) + haversine_m(a_end, b_start)) / 2.0
    same = (haversine_m(a_start, b_start) + haversine_m(a_end, b_end)) / 2.0
    cross_s = score_from_distance(cross, good_m=120.0, bad_m=2000.0)
    same_s = score_from_distance(same, good_m=120.0, bad_m=2000.0)
    if cross_s is None or same_s is None:
        return None
    return clip01(0.5 + 0.5 * (float(cross_s) - float(same_s)))


def route_loop_suspicion(line: Sequence[LonLat], *, loop_close_m: float = 260.0) -> Optional[float]:
    pts = list(line or [])
    if len(pts) < 3:
        return None
    length_m = polyline_length_m(pts)
    if length_m <= 0:
        return None
    end_gap = haversine_m(pts[0], pts[-1])
    close_score = score_from_distance(end_gap, good_m=20.0, bad_m=float(loop_close_m))
    if close_score is None:
        return None
    if length_m < 600.0:
        return clip01(float(close_score) * 0.3)
    return clip01(float(close_score))


def reverse_progression_score(
    stop_points: Sequence[LonLat],
    corridor_line: Sequence[LonLat],
) -> Optional[float]:
    stops = list(stop_points or [])
    line = list(corridor_line or [])
    if len(stops) < 3 or len(line) < 2:
        return None
    fracs: List[float] = []
    for p in stops:
        proj = project_point_fraction_on_polyline(p, line)
        if proj is None:
            continue
        fracs.append(float(proj[0]))
    if len(fracs) < 3:
        return None

    reversed_pairs = 0
    total_pairs = 0
    for i in range(1, len(fracs)):
        total_pairs += 1
        if fracs[i] <= fracs[i - 1] + 1e-6:
            reversed_pairs += 1
    if total_pairs <= 0:
        return None
    return clip01(float(reversed_pairs) / float(total_pairs))


def extract_endpoint_tokens(text: Any) -> List[str]:
    norm = normalize_text(text)
    if not norm:
        return []
    parts = re.split(r"\b(?:to|a|hasta|desde|via|versus|vs|\-|/|>|<)\b", norm)
    cleaned = [re.sub(r"\s+", " ", p).strip() for p in parts if p and p.strip()]
    if len(cleaned) >= 2:
        return [cleaned[0], cleaned[-1]]
    toks = tokenize(norm)
    if len(toks) >= 4:
        mid = len(toks) // 2
        return [" ".join(toks[:mid]).strip(), " ".join(toks[mid:]).strip()]
    return []


def endpoint_swap_name_similarity(name_a: Any, name_b: Any) -> Optional[float]:
    ea = extract_endpoint_tokens(name_a)
    eb = extract_endpoint_tokens(name_b)
    if len(ea) < 2 or len(eb) < 2:
        return None
    s1 = text_similarity(ea[0], eb[1])
    s2 = text_similarity(ea[1], eb[0])
    if s1 is None or s2 is None:
        return None
    return clip01((float(s1) + float(s2)) / 2.0)


def _direction_groups_from_text(text: Any) -> set[str]:
    toks = set(tokenize(text))
    groups: set[str] = set()
    for group, words in _DIRECTION_WORD_GROUPS.items():
        if toks.intersection(words):
            groups.add(group)
    return groups


def direction_word_conflict_flag(text_a: Any, text_b: Any) -> bool:
    ga = _direction_groups_from_text(text_a)
    gb = _direction_groups_from_text(text_b)
    if not ga or not gb:
        return False
    if ga.intersection(gb):
        return True
    for g in ga:
        opposite = _OPPOSITE_GROUPS.get(g)
        if opposite and opposite in gb:
            return False
    return False


def name_family_match_score(name_a: Any, name_b: Any) -> Optional[float]:
    a = [t for t in tokenize(name_a) if t not in {"to", "via", "inbound", "outbound"}]
    b = [t for t in tokenize(name_b) if t not in {"to", "via", "inbound", "outbound"}]
    if not a or not b:
        return None
    if len(a) > 6:
        a = a[:6]
    if len(b) > 6:
        b = b[:6]
    return token_jaccard(" ".join(a), " ".join(b))
