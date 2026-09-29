# phase4_semantics/ingest/overpass/normalize_to_evidence.py
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(x: Optional[str]) -> Optional[str]:
    if x is None:
        return None
    s = str(x).strip()
    if not s:
        return None
    # basic cleanup
    s = " ".join(s.split())
    return s


def _as_list(x: Any) -> List[str]:
    if x is None:
        return []
    if isinstance(x, list):
        return [str(v) for v in x if _clean_text(str(v)) is not None]
    return [str(x)]


def _cap01(v: float) -> float:
    if v < 0:
        return 0.0
    if v > 1:
        return 1.0
    return v


# ------------------------------------------------------------
# Evidence Record (normalized output)
# ------------------------------------------------------------

@dataclass
class EvidenceRecord:
    """
    Normalized evidence object that you can:
      - insert into semantics.route_evidence_records
      - log as JSONL
      - feed into matcher/compiler

    Keep it source-agnostic.
    """
    record_id: str
    source_type: str                 # e.g. "osm_overpass_seed"
    source_ref: str                  # e.g. "osm:relation:123456"
    confidence_hint: float           # 0..1
    extracted: Dict[str, Any]        # normalized extracted fields
    raw: Dict[str, Any]              # raw source payload (or subset)
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "record_id": self.record_id,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "confidence_hint": float(self.confidence_hint),
            "extracted": self.extracted,
            "raw": self.raw,
            "created_at": self.created_at,
        }


# ------------------------------------------------------------
# Overpass-specific extraction
# ------------------------------------------------------------

def _find_relation_element(resp: Dict[str, Any], relation_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    In an Overpass response, find the relation element.
    If relation_id is None, return the first relation found.
    """
    for el in resp.get("elements", []):
        if el.get("type") != "relation":
            continue
        if relation_id is None:
            return el
        try:
            if int(el.get("id")) == int(relation_id):
                return el
        except Exception:
            continue
    return None


def extract_relation_tags(resp: Dict[str, Any], relation_id: Optional[int] = None) -> Dict[str, str]:
    rel = _find_relation_element(resp, relation_id=relation_id)
    if not rel:
        return {}
    tags = rel.get("tags") or {}
    return {str(k): str(v) for k, v in tags.items()}


def summarize_members(resp: Dict[str, Any]) -> Dict[str, Any]:
    rels = 0
    ways = 0
    nodes = 0

    for el in resp.get("elements", []):
        t = el.get("type")
        if t == "relation":
            rels += 1
        elif t == "way":
            ways += 1
        elif t == "node":
            nodes += 1

    return {"relations": rels, "ways": ways, "nodes": nodes}


def extract_naming_fields(tags: Dict[str, str]) -> Dict[str, Any]:
    """
    Convert OSM tags into normalized naming fields for Phase 4.
    We keep a stable schema no matter the source.
    """
    name = _clean_text(tags.get("name"))
    ref = _clean_text(tags.get("ref"))
    operator_ = _clean_text(tags.get("operator"))
    network = _clean_text(tags.get("network"))
    from_ = _clean_text(tags.get("from"))
    to_ = _clean_text(tags.get("to"))

    # route tag sometimes exists (route=bus)
    route_type = _clean_text(tags.get("route"))

    # Some feeds use alt_name / official_name
    alt_name = _clean_text(tags.get("alt_name"))
    official_name = _clean_text(tags.get("official_name"))
    short_name = _clean_text(tags.get("short_name"))

    # Basic alias generation
    aliases: List[str] = []
    for v in [ref, short_name, official_name, alt_name, name]:
        if v and v not in aliases:
            aliases.append(v)

    # A "label" is what your UI shows prominently
    # prefer "ref + name" if both exist
    label = None
    if ref and name:
        label = f"{ref} {name}"
    elif ref:
        label = ref
    elif name:
        label = name

    return {
        "label": label,
        "name": name,
        "ref": ref,
        "aliases": aliases,
        "operator": operator_,
        "network": network,
        "from": from_,
        "to": to_,
        "route_type": route_type,
        "tags_raw": tags,
    }


def compute_confidence_hint(
    naming: Dict[str, Any],
    overlap_ratio: Optional[float] = None,
    overlap_count: Optional[int] = None,
) -> float:
    """
    This is NOT ML.
    It's just a scalar hint that helps sorting evidence before matching/training.

    - if you already computed overlap_ratio from stop-node intersection, use it (strongest signal)
    - otherwise, infer from how rich the relation tags are
    """
    if overlap_ratio is not None:
        return _cap01(float(overlap_ratio))

    # heuristic from fields
    score = 0.45
    if naming.get("ref"):
        score += 0.20
    if naming.get("name"):
        score += 0.15
    if naming.get("operator") or naming.get("network"):
        score += 0.10
    if naming.get("from") and naming.get("to"):
        score += 0.05

    # tiny bump if it has many aliases
    aliases = naming.get("aliases") or []
    if len(aliases) >= 3:
        score += 0.03

    # optional bump if overlap_count is big (even without ratio)
    if overlap_count is not None:
        try:
            oc = int(overlap_count)
            if oc >= 30:
                score += 0.05
            elif oc >= 10:
                score += 0.02
        except Exception:
            pass

    return _cap01(score)


# ------------------------------------------------------------
# Normalizers (raw Overpass -> EvidenceRecord)
# ------------------------------------------------------------

def normalize_overpass_relation_to_evidence(
    overpass_resp: Dict[str, Any],
    relation_id: Optional[int] = None,
    source_type: str = "osm_overpass_seed",
    overlap_ratio: Optional[float] = None,
    overlap_count: Optional[int] = None,
    route_id_hint: Optional[str] = None,
) -> EvidenceRecord:
    """
    Main converter.

    overpass_resp:
      - output of fetch_relation_members.py (relation + > members)
      - OR tags-only response (still has relation element with tags)

    You can optionally pass overlap_ratio/count (from max intersection step).
    """
    rid = int(relation_id) if relation_id is not None else None
    tags = extract_relation_tags(overpass_resp, relation_id=rid)
    naming = extract_naming_fields(tags)
    summary = summarize_members(overpass_resp)

    # strongest possible ref for Overpass evidence
    rel_id_actual = None
    rel_el = _find_relation_element(overpass_resp, relation_id=rid)
    if rel_el and "id" in rel_el:
        try:
            rel_id_actual = int(rel_el["id"])
        except Exception:
            rel_id_actual = rid

    source_ref = f"osm:relation:{rel_id_actual}" if rel_id_actual else "osm:relation:unknown"
    conf = compute_confidence_hint(naming, overlap_ratio=overlap_ratio, overlap_count=overlap_count)

    extracted = {
        "kind": "route_relation",
        "relation_id": rel_id_actual,
        "route_id_hint": route_id_hint,
        "naming": naming,
        "member_summary": summary,
        "intersection": {
            "overlap_ratio": float(overlap_ratio) if overlap_ratio is not None else None,
            "overlap_count": int(overlap_count) if overlap_count is not None else None,
        },
    }

    # raw can be big → keep only minimal but still useful
    raw = {
        "overpass": {
            "relation_id": rel_id_actual,
            "tags": tags,
            "member_summary": summary,
        }
    }

    return EvidenceRecord(
        record_id=str(uuid4()),
        source_type=str(source_type),
        source_ref=source_ref,
        confidence_hint=float(conf),
        extracted=extracted,
        raw=raw,
        created_at=_now_iso(),
    )


def normalize_seed_candidate_to_evidence(seed_candidate: Dict[str, Any]) -> EvidenceRecord:
    """
    If your seed_candidates.py produces rows like:

      {
        "route_id": "...",
        "relation_id": 123,
        "overpass_raw": {...},          # optional
        "relation_tags": {...},         # optional
        "overlap_count": 45,
        "overlap_ratio": 0.82
      }

    This turns it into an EvidenceRecord cleanly.
    """
    route_id = seed_candidate.get("route_id")
    relation_id = seed_candidate.get("relation_id")

    raw = seed_candidate.get("overpass_raw")
    if raw is None:
        # tags-only mode: wrap tags into a minimal pseudo-overpass response
        tags = seed_candidate.get("relation_tags") or {}
        raw = {
            "elements": [
                {"type": "relation", "id": relation_id, "tags": tags}
            ]
        }

    return normalize_overpass_relation_to_evidence(
        overpass_resp=raw,
        relation_id=relation_id,
        source_type=str(seed_candidate.get("source_type") or "osm_overpass_seed"),
        overlap_ratio=seed_candidate.get("overlap_ratio"),
        overlap_count=seed_candidate.get("overlap_count"),
        route_id_hint=route_id,
    )


# ------------------------------------------------------------
# JSONL utilities (debug + offline pipelines)
# ------------------------------------------------------------

def read_json_any(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        txt = f.read().strip()
        if not txt:
            return None
        if txt.startswith("{") or txt.startswith("["):
            return json.loads(txt)
    return None


def read_jsonl(path: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            out.append(json.loads(s))
    return out


def write_jsonl(path: str, rows: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ------------------------------------------------------------
# CLI
# ------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Normalize Overpass relation output into Phase4 evidence_record JSONL."
    )
    ap.add_argument("--input", required=True, help="Input JSON / JSONL file")
    ap.add_argument("--out", default="overpass_evidence.jsonl", help="Output JSONL evidence file")
    ap.add_argument("--relation-id", type=int, default=None, help="Relation id (optional)")
    ap.add_argument("--route-id-hint", type=str, default=None, help="Route id hint (optional)")
    ap.add_argument("--source-type", type=str, default="osm_overpass_seed", help="Evidence source_type")

    args = ap.parse_args()

    # If input is JSONL, assume it's seed candidates
    if args.input.endswith(".jsonl"):
        rows = read_jsonl(args.input)
        evidence_rows: List[Dict[str, Any]] = []
        for r in rows:
            ev = normalize_seed_candidate_to_evidence(r)
            evidence_rows.append(ev.to_dict())
        write_jsonl(args.out, evidence_rows)
        print(f"✅ Wrote {len(evidence_rows)} evidence records -> {args.out}")
        return

    # Otherwise JSON: treat as raw overpass relation response
    raw = read_json_any(args.input)
    if raw is None:
        raise RuntimeError(f"Could not read JSON from: {args.input}")

    ev = normalize_overpass_relation_to_evidence(
        overpass_resp=raw,
        relation_id=args.relation_id,
        source_type=args.source_type,
        route_id_hint=args.route_id_hint,
    )
    write_jsonl(args.out, [ev.to_dict()])
    print(f"✅ Wrote 1 evidence record -> {args.out}")


if __name__ == "__main__":
    main()
