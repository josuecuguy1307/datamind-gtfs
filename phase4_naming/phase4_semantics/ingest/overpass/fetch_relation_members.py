# phase4_semantics/ingest/overpass/fetch_relation_members.py
from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any, Dict, Optional, Set, Tuple

try:
    import requests
except Exception:
    requests = None


DEFAULT_OVERPASS = os.getenv("OVERPASS_URL", "http://127.0.0.1:12346/api/interpreter")


# ============================================================
# Overpass query builders
# ============================================================

def build_relation_members_query(relation_id: int) -> str:
    """
    Fetch relation body + all members recursively.
    `>;` expands relation -> ways -> nodes
    """
    return f"""
[out:json][timeout:25];
relation({relation_id});
out body;
>;
out skel qt;
""".strip()


def build_relation_tags_only_query(relation_id: int) -> str:
    """
    Tags only (cheap).
    Useful if you just want name/ref/operator without members.
    """
    return f"""
[out:json][timeout:25];
relation({relation_id});
out tags;
""".strip()


# ============================================================
# HTTP client
# ============================================================

def overpass_post(
    query: str,
    overpass_url: str = DEFAULT_OVERPASS,
    timeout_s: int = 45,
    sleep_s: float = 0.0,
) -> Dict[str, Any]:
    """
    Sends Overpass QL query to Overpass API.

    - sleep_s: optional small delay to avoid rate-limit bursts.
    """
    if requests is None:
        raise RuntimeError("requests is not installed. Install it with: pip install requests")

    if sleep_s > 0:
        time.sleep(float(sleep_s))

    resp = requests.post(overpass_url, data=query.encode("utf-8"), timeout=timeout_s)
    if resp.status_code != 200:
        raise RuntimeError(f"Overpass HTTP {resp.status_code}: {resp.text[:800]}")
    return resp.json()


# ============================================================
# Extractors
# ============================================================

def get_relation_tags(resp: Dict[str, Any], relation_id: Optional[int] = None) -> Dict[str, str]:
    """
    Extract tags from the relation element itself.
    """
    rid = int(relation_id) if relation_id is not None else None

    for el in resp.get("elements", []):
        if el.get("type") == "relation":
            if rid is None or int(el.get("id", -1)) == rid:
                tags = el.get("tags") or {}
                # ensure string dict
                return {str(k): str(v) for k, v in tags.items()}
    return {}


def extract_relation_nodes(resp: Dict[str, Any]) -> Set[int]:
    """
    Returns all node IDs present in the expanded response.
    """
    out: Set[int] = set()
    for el in resp.get("elements", []):
        if el.get("type") == "node" and "id" in el:
            try:
                out.add(int(el["id"]))
            except Exception:
                continue
    return out


def extract_relation_ways(resp: Dict[str, Any]) -> Set[int]:
    """
    Returns all way IDs present in the expanded response.
    """
    out: Set[int] = set()
    for el in resp.get("elements", []):
        if el.get("type") == "way" and "id" in el:
            try:
                out.add(int(el["id"]))
            except Exception:
                continue
    return out


def extract_relation_members_summary(resp: Dict[str, Any]) -> Dict[str, Any]:
    """
    Quick summary counts (useful for logging/debug).
    """
    nodes = extract_relation_nodes(resp)
    ways = extract_relation_ways(resp)

    rel_count = 0
    for el in resp.get("elements", []):
        if el.get("type") == "relation":
            rel_count += 1

    return {
        "relations": rel_count,
        "ways": len(ways),
        "nodes": len(nodes),
    }


# ============================================================
# Main fetch function
# ============================================================

def fetch_relation_members(
    relation_id: int,
    overpass_url: str = DEFAULT_OVERPASS,
    timeout_s: int = 45,
    sleep_s: float = 0.0,
) -> Dict[str, Any]:
    """
    Fetch full relation + expanded members (ways/nodes).
    Returns Overpass JSON.
    """
    q = build_relation_members_query(int(relation_id))
    return overpass_post(q, overpass_url=overpass_url, timeout_s=timeout_s, sleep_s=sleep_s)


def fetch_relation_tags_only(
    relation_id: int,
    overpass_url: str = DEFAULT_OVERPASS,
    timeout_s: int = 45,
    sleep_s: float = 0.0,
) -> Dict[str, Any]:
    """
    Fetch only tags (cheap).
    """
    q = build_relation_tags_only_query(int(relation_id))
    return overpass_post(q, overpass_url=overpass_url, timeout_s=timeout_s, sleep_s=sleep_s)


# ============================================================
# CLI (super useful to debug relation manually)
# ============================================================

def _save_json(path: str, obj: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser(description="Fetch Overpass relation members (relation + ways + nodes).")
    ap.add_argument("--relation-id", required=True, type=int, help="OSM relation id")
    ap.add_argument("--out", default="relation_members.json", help="Output JSON file")
    ap.add_argument("--tags-only", action="store_true", help="Fetch tags only (no members)")
    ap.add_argument("--overpass-url", default=DEFAULT_OVERPASS, help="Overpass endpoint URL")
    ap.add_argument("--sleep", type=float, default=0.0, help="Sleep seconds before request")
    ap.add_argument("--timeout", type=int, default=45, help="Request timeout seconds")

    args = ap.parse_args()

    if args.tags_only:
        resp = fetch_relation_tags_only(
            relation_id=args.relation_id,
            overpass_url=args.overpass_url,
            timeout_s=args.timeout,
            sleep_s=args.sleep,
        )
        tags = get_relation_tags(resp, relation_id=args.relation_id)
        print("✅ Relation tags:", tags)
        _save_json(args.out, {"relation_id": args.relation_id, "tags": tags, "raw": resp})
        print(f"✅ Saved tags JSON: {args.out}")
        return

    resp = fetch_relation_members(
        relation_id=args.relation_id,
        overpass_url=args.overpass_url,
        timeout_s=args.timeout,
        sleep_s=args.sleep,
    )

    tags = get_relation_tags(resp, relation_id=args.relation_id)
    summary = extract_relation_members_summary(resp)

    print("✅ Relation tags:", tags)
    print("✅ Summary:", summary)

    _save_json(args.out, resp)
    print(f"✅ Saved full relation JSON: {args.out}")


if __name__ == "__main__":
    main()
