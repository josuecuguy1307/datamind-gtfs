from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional


@dataclass
class DocRow:
    route_ref: Optional[str]
    route_name: Optional[str]
    operator: Optional[str]
    from_place: Optional[str]
    to_place: Optional[str]
    via: List[str]
    service_type: Optional[str]
    direction_hint: Optional[str]
    raw_text: Optional[str]
    page: Optional[int]
    confidence: Optional[float]


@dataclass
class EvidenceRecord:
    source_type: str
    source_id: Optional[str]
    source_title: Optional[str]

    route_ref: Optional[str]
    route_name: Optional[str]
    operator: Optional[str]
    from_place: Optional[str]
    to_place: Optional[str]

    aliases: List[str]
    tags: List[str]

    confidence_hint: Optional[float]
    raw: Dict[str, Any]


def _clamp01(x: Optional[float]) -> Optional[float]:
    if x is None:
        return None
    try:
        v = float(x)
    except Exception:
        return None
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def _norm_str(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    s2 = str(s).strip()
    return s2 if s2 else None


def doc_rows_to_evidence_records(
    rows: List[DocRow],
    source_type: str,
    source_id: Optional[str] = None,
    source_title: Optional[str] = None,
    default_tags: Optional[List[str]] = None,
) -> List[EvidenceRecord]:
    """
    Converts extracted DocRow objects into EvidenceRecord objects
    suitable for insertion into semantics.route_evidence_records.
    """

    out: List[EvidenceRecord] = []
    default_tags = default_tags or []

    for r in rows:
        route_ref = _norm_str(r.route_ref)
        route_name = _norm_str(r.route_name)
        operator = _norm_str(r.operator)
        from_place = _norm_str(r.from_place)
        to_place = _norm_str(r.to_place)

        aliases: List[str] = []
        # alias heuristics for V1:
        # - if ref exists, treat it as alias
        # - if name exists, also alias it
        if route_ref:
            aliases.append(route_ref)
        if route_name and route_name not in aliases:
            aliases.append(route_name)

        tags = list(dict.fromkeys([*(default_tags or [])]))  # unique preserve order
        if r.service_type:
            tags.append(str(r.service_type).strip())
        tags = [t for t in tags if t]

        conf = _clamp01(r.confidence)
        raw = {
            "doc_row": asdict(r),
        }

        out.append(
            EvidenceRecord(
                source_type=source_type,
                source_id=source_id,
                source_title=source_title,
                route_ref=route_ref,
                route_name=route_name,
                operator=operator,
                from_place=from_place,
                to_place=to_place,
                aliases=aliases,
                tags=tags,
                confidence_hint=conf,
                raw=raw,
            )
        )

    return out


def _load_schema_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_doc_rows_with_openai(
    file_id: str,
    *,
    source_title: Optional[str] = None,
    instructions: Optional[str] = None,
    model: Optional[str] = None,
    schema_path: Optional[str] = None,
) -> List[DocRow]:
    """
    Uses OpenAI to extract structured doc rows from a PDF/Excel file_id.
    Returns a list[DocRow].

    Requirements:
      - OPENAI_API_KEY set
      - openai python package installed

    This is the hardest part: turning messy tables into consistent rows.
    Structured outputs makes it deterministic.
    """

    # Lazy import so pipeline doesn't crash if user hasn't installed it yet
    try:
        from openai import OpenAI
    except Exception as e:
        raise RuntimeError(
            "Missing dependency: openai. Install with: pip install openai"
        ) from e

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY in environment")

    client = OpenAI(api_key=api_key)

    model = model or os.getenv("OPENAI_EXTRACT_MODEL", "gpt-4o-mini")

    # schema file location default = same folder
    if schema_path is None:
        schema_path = os.path.join(os.path.dirname(__file__), "doc_row.schema.json")

    schema_obj = _load_schema_file(schema_path)
    json_schema = schema_obj.get("schema")

    # Prompting strategy:
    # - Extract only routes rows (not paragraphs)
    # - Keep nulls when unknown
    # - Use via[] if row mentions intermediate places
    base_instructions = """
You are extracting transit route table rows from a document (PDF/Excel).
Return ONLY structured JSON following the schema.
Rules:
- Each row corresponds to one route entry.
- If a field is missing, return null.
- route_ref is the short code/number if present.
- from_place and to_place should be clear endpoints if present.
- via is a list of intermediate places if present, else [].
- confidence is 0..1 based on extraction clarity.
"""

    if instructions:
        base_instructions += "\n" + instructions.strip()

    if source_title:
        base_instructions += f"\nDocument title: {source_title}\n"

    # Responses API structured outputs style
    resp = client.responses.create(
        model=model,
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": base_instructions},
                    {"type": "input_file", "file_id": file_id},
                ],
            }
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": schema_obj.get("name", "doc_rows_schema"),
                "schema": json_schema,
                "strict": True,
            },
        },
    )

    # Parse output
    # The SDK returns parsed JSON in different ways depending on version.
    # We'll handle both common patterns safely.
    parsed = None

    if hasattr(resp, "output") and resp.output:
        # Most common new style:
        # resp.output[0].content[0].parsed
        try:
            parsed = resp.output[0].content[0].parsed
        except Exception:
            pass

        # fallback: maybe text -> json
        if parsed is None:
            try:
                txt = resp.output[0].content[0].text
                parsed = json.loads(txt)
            except Exception:
                pass

    if parsed is None:
        raise RuntimeError("Could not parse OpenAI structured response.")

    rows_raw = parsed.get("rows", [])
    out: List[DocRow] = []

    for r in rows_raw:
        out.append(
            DocRow(
                route_ref=r.get("route_ref"),
                route_name=r.get("route_name"),
                operator=r.get("operator"),
                from_place=r.get("from_place"),
                to_place=r.get("to_place"),
                via=r.get("via") or [],
                service_type=r.get("service_type"),
                direction_hint=r.get("direction_hint"),
                raw_text=r.get("raw_text"),
                page=r.get("page"),
                confidence=_clamp01(r.get("confidence")),
            )
        )

    return out
