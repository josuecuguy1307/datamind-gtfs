# phase4_semantics/ingest/gis/fetch_arcgis.py
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union
from urllib.parse import urljoin

import requests


# ------------------------------------------------------------
# Core idea:
#   ArcGIS REST endpoints look like:
#     https://.../FeatureServer
#     https://.../MapServer
#   Layers are:
#     https://.../FeatureServer/0
#     https://.../FeatureServer/1
#   Query endpoint:
#     https://.../FeatureServer/0/query?f=json&...
#
# We fetch "features" as raw records that are later normalized
# into semantics.route_evidence_records.
# ------------------------------------------------------------


DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 3
DEFAULT_SLEEP = 0.6


def _clean_url(u: str) -> str:
    return u.rstrip("/")


def _http_get_json(url: str, params: Dict[str, Any], timeout: int = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _get_json_with_retries(
    url: str,
    params: Dict[str, Any],
    retries: int = DEFAULT_RETRIES,
    timeout: int = DEFAULT_TIMEOUT,
    sleep_s: float = DEFAULT_SLEEP,
) -> Dict[str, Any]:
    last_err: Optional[Exception] = None
    for i in range(retries):
        try:
            return _http_get_json(url, params=params, timeout=timeout)
        except Exception as e:
            last_err = e
            time.sleep(sleep_s * (2 ** i))
    raise RuntimeError(f"ArcGIS request failed after {retries} retries: {url} :: {last_err}")


def _is_feature_or_map_server(url: str) -> bool:
    u = url.lower()
    return u.endswith("/featureserver") or u.endswith("/mapserver")


def _layer_url(server_url: str, layer_id: Union[int, str]) -> str:
    return f"{_clean_url(server_url)}/{layer_id}"


def _query_url(layer_url: str) -> str:
    return f"{_clean_url(layer_url)}/query"


def _pick_first_nonempty(attrs: Dict[str, Any], keys: Iterable[str]) -> Optional[str]:
    for k in keys:
        if k in attrs and attrs[k] is not None:
            v = str(attrs[k]).strip()
            if v != "":
                return v
    return None


def _as_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


@dataclass
class ArcGISLayer:
    id: int
    name: str
    geometry_type: Optional[str] = None
    fields: Optional[List[Dict[str, Any]]] = None


class ArcGISClient:
    def __init__(self, server_url: str, token: Optional[str] = None):
        self.server_url = _clean_url(server_url)
        self.token = token

        if not _is_feature_or_map_server(self.server_url):
            raise ValueError(
                f"ArcGIS server_url must end in /FeatureServer or /MapServer. Got: {self.server_url}"
            )

    def _base_params(self) -> Dict[str, Any]:
        p = {"f": "json"}
        if self.token:
            p["token"] = self.token
        return p

    def service_info(self) -> Dict[str, Any]:
        return _get_json_with_retries(self.server_url, params=self._base_params())

    def list_layers(self) -> List[ArcGISLayer]:
        info = self.service_info()
        layers = info.get("layers", []) or []
        out: List[ArcGISLayer] = []
        for L in layers:
            out.append(
                ArcGISLayer(
                    id=int(L.get("id")),
                    name=str(L.get("name", "")),
                    geometry_type=L.get("geometryType"),
                    fields=L.get("fields"),
                )
            )
        return out

    def layer_info(self, layer_id: Union[int, str]) -> Dict[str, Any]:
        lurl = _layer_url(self.server_url, layer_id)
        return _get_json_with_retries(lurl, params=self._base_params())

    def query_features(
        self,
        layer_id: Union[int, str],
        bbox: Optional[Tuple[float, float, float, float]] = None,
        where: str = "1=1",
        out_fields: str = "*",
        return_geometry: bool = True,
        out_sr: int = 4326,
        max_per_page: int = 2000,
        limit_total: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Returns raw ArcGIS features:
          { "attributes": {...}, "geometry": {...} }
        Handles pagination via resultOffset/resultRecordCount.
        """
        layer_url = _layer_url(self.server_url, layer_id)
        qurl = _query_url(layer_url)

        base = self._base_params()
        base.update(
            {
                "where": where,
                "outFields": out_fields,
                "returnGeometry": "true" if return_geometry else "false",
                "outSR": out_sr,
                "f": "json",
            }
        )

        if bbox is not None:
            # ArcGIS expects: geometry=minx,miny,maxx,maxy with geometryType=esriGeometryEnvelope
            minx, miny, maxx, maxy = bbox
            base["geometry"] = f"{minx},{miny},{maxx},{maxy}"
            base["geometryType"] = "esriGeometryEnvelope"
            base["spatialRel"] = "esriSpatialRelIntersects"

        all_feats: List[Dict[str, Any]] = []
        offset = 0

        while True:
            params = dict(base)
            params["resultOffset"] = offset
            params["resultRecordCount"] = max_per_page

            payload = _get_json_with_retries(qurl, params=params)

            if "error" in payload:
                raise RuntimeError(f"ArcGIS query error: {payload['error']}")

            feats = payload.get("features", []) or []
            all_feats.extend(feats)

            if limit_total is not None and len(all_feats) >= limit_total:
                return all_feats[:limit_total]

            # If fewer than requested, we're done
            if len(feats) < max_per_page:
                break

            offset += max_per_page

        return all_feats


# ------------------------------------------------------------
# Convert ArcGIS features -> Phase4 raw records
# (Normalization happens later in normalize_to_evidence.py)
# ------------------------------------------------------------

def arcgis_features_to_raw_records(
    features: List[Dict[str, Any]],
    source_url: str,
    layer_id: Union[int, str],
    layer_name: Optional[str] = None,
    field_hints: Optional[Dict[str, List[str]]] = None,
) -> List[Dict[str, Any]]:
    """
    Converts ArcGIS feature payloads into generic "raw ingestion records"
    that are easy to normalize.

    field_hints can specify attribute keys to map into semantic slots:
      {
        "route_name": ["ROUTE", "route_name", "name", "NOMBRE"],
        "route_ref":  ["REF", "ref", "CODIGO"],
        "operator":   ["OPERADOR", "operator", "COOPERATIVA"],
        "from":       ["FROM", "from", "ORIGEN"],
        "to":         ["TO", "to", "DESTINO"],
      }
    """
    hints = field_hints or {}
    name_keys = hints.get("route_name", ["route_name", "name", "NOMBRE", "ROUTE"])
    ref_keys = hints.get("route_ref", ["route_ref", "ref", "REF", "CODIGO", "CODE"])
    op_keys = hints.get("operator", ["operator", "OPERADOR", "COOPERATIVA", "AGENCY"])
    from_keys = hints.get("from", ["from", "FROM", "ORIGEN", "start", "START"])
    to_keys = hints.get("to", ["to", "TO", "DESTINO", "end", "END"])

    out: List[Dict[str, Any]] = []
    for f in features:
        attrs = f.get("attributes", {}) or {}
        geom = f.get("geometry")

        record = {
            "source_type": "official_gis_arcgis",
            "source_url": source_url,
            "layer_id": str(layer_id),
            "layer_name": layer_name or "",
            "raw_attributes": attrs,
            "raw_geometry": geom,

            # soft mapped semantic hints (optional, can be empty)
            "hint_route_name": _pick_first_nonempty(attrs, name_keys),
            "hint_route_ref": _pick_first_nonempty(attrs, ref_keys),
            "hint_operator": _pick_first_nonempty(attrs, op_keys),
            "hint_from": _pick_first_nonempty(attrs, from_keys),
            "hint_to": _pick_first_nonempty(attrs, to_keys),
        }
        out.append(record)

    return out


def save_jsonl(rows: List[Dict[str, Any]], out_path: str) -> None:
    p = out_path
    with open(p, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ------------------------------------------------------------
# CLI
# ------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Fetch data from ArcGIS FeatureServer/MapServer and emit raw records")
    ap.add_argument("--url", required=True, help="ArcGIS server URL ending in /FeatureServer or /MapServer")
    ap.add_argument("--layer", required=False, type=int, default=None, help="Layer id (e.g. 0)")
    ap.add_argument("--list-layers", action="store_true", help="List layers and exit")

    ap.add_argument("--bbox", nargs=4, type=float, default=None, metavar=("MINX", "MINY", "MAXX", "MAXY"))
    ap.add_argument("--where", default="1=1")
    ap.add_argument("--out-fields", default="*")
    ap.add_argument("--max-per-page", type=int, default=2000)
    ap.add_argument("--limit", type=int, default=None)

    ap.add_argument("--token", default=None, help="Optional ArcGIS token")
    ap.add_argument("--out", default="arcgis_raw.jsonl", help="Output JSONL file")

    args = ap.parse_args()

    client = ArcGISClient(args.url, token=args.token)

    if args.list_layers:
        layers = client.list_layers()
        print("\nLayers:")
        for L in layers:
            print(f"- id={L.id}  name={L.name}  geom={L.geometry_type}")
        print("")
        return

    if args.layer is None:
        raise SystemExit("You must provide --layer (or use --list-layers first).")

    layer_info = client.layer_info(args.layer)
    layer_name = layer_info.get("name", "")

    bbox = tuple(args.bbox) if args.bbox else None

    feats = client.query_features(
        layer_id=args.layer,
        bbox=bbox,                      # None = whole layer
        where=args.where,
        out_fields=args.out_fields,
        return_geometry=True,
        out_sr=4326,
        max_per_page=args.max_per_page,
        limit_total=args.limit,
    )

    rows = arcgis_features_to_raw_records(
        feats,
        source_url=args.url,
        layer_id=args.layer,
        layer_name=layer_name,
        field_hints=None,  # you can pass your own mapping later
    )

    save_jsonl(rows, args.out)
    print(f"\n✅ Saved {len(rows)} raw ArcGIS records to: {args.out}\n")


if __name__ == "__main__":
    main()
