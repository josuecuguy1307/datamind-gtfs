# datamind_console/ui/widgets/phase3/_geom.py
from __future__ import annotations

from typing import List, Tuple, Dict, Any, Optional

LonLat = Tuple[float, float]  # (lon, lat)

def parse_linestring_wkt(wkt: str) -> List[LonLat]:
    if not wkt:
        return []
    s = wkt.strip()
    su = s.upper()

    if su.startswith("LINESTRING"):
        inner = s[s.find("(") + 1 : s.rfind(")")]
        pts: List[LonLat] = []
        for part in inner.split(","):
            part = part.strip()
            if not part:
                continue
            a, b = part.split()[:2]
            pts.append((float(a), float(b)))
        return pts

    if su.startswith("MULTILINESTRING"):
        inner = s[s.find("(") + 1 : s.rfind(")")]
        pts: List[LonLat] = []
        chunks = inner.replace("),(", ")|(").split("|")
        for ch in chunks:
            ch = ch.strip().strip("(").strip(")")
            if not ch:
                continue
            for part in ch.split(","):
                part = part.strip()
                if not part:
                    continue
                a, b = part.split()[:2]
                pts.append((float(a), float(b)))
        return pts

    return []

def line_to_points_df(line: List[LonLat]) -> List[Dict[str, float]]:
    # st.map expects lat/lon keys
    return [{"lon": float(lon), "lat": float(lat)} for (lon, lat) in line]

def safe_float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None
