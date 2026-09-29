from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

from datamind_console.common.geography_input_resolver import normalize_geographic_text


_GENERIC_ROUTE_HINT_NAMES = {
    "bus",
    "corridor",
    "line",
    "linea",
    "route",
    "ruta",
    "servicio",
}

_TRANSPORT_MODE_WORDS = {
    "bus", "buses", "autobus", "microbus", "minibus",
    "metro", "metrobus", "metrobus",
    "trole", "trolebus", "trolley",
    "tren", "train", "ferrocarril",
    "brt", "tranvia", "tram",
    "alimentador", "feeder",
    "interparroquial", "intercantonal", "interprovincial",
}

_COOPERATIVE_SIGNAL_WORDS = {
    "cooperativa", "coop", "cooperativa de transporte",
    "compania", "empresa", "consorcio", "operadora",
    "sociedad", "asociacion", "transporte",
}

_CORRIDOR_DASH_RE = re.compile(
    r"^([A-Za-zÀ-ÿ0-9\s]+)\s*[-–—]\s*([A-Za-zÀ-ÿ0-9\s]+)$"
)


def _normalize_ref_alias(text: Any) -> str:
    return normalize_geographic_text(text).replace(" ", "")


def _build_ref_alias_map(ref_catalog: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for row in list(ref_catalog or []):
        item = dict(row or {})
        key = str(item.get("key") or item.get("ref_id") or "").strip()
        label = str(item.get("label") or "").strip()
        if not key:
            continue
        for alias in (key, label):
            normalized = _normalize_ref_alias(alias)
            if normalized:
                out.setdefault(normalized, key)
    return out


def _looks_like_ref_token(token: Any) -> Optional[str]:
    cleaned = re.sub(r"[^A-Za-z0-9-]", "", str(token or "").strip())
    if not cleaned:
        return None
    if re.fullmatch(r"[A-Za-z]{1,6}\d{1,4}[A-Za-z0-9-]*", cleaned):
        return cleaned.upper()
    if re.fullmatch(r"\d{1,4}[A-Za-z]{0,4}", cleaned):
        return cleaned.upper()
    return None


def _coerce_ref_token(token: Any, *, alias_map: Dict[str, str]) -> Optional[str]:
    normalized = _normalize_ref_alias(token)
    if not normalized:
        return None
    mapped = alias_map.get(normalized)
    if mapped:
        return mapped
    return _looks_like_ref_token(token)


def _is_transport_mode_word(word: str) -> bool:
    return normalize_geographic_text(word) in _TRANSPORT_MODE_WORDS


def _extract_cooperative_signal(words: List[str]) -> Optional[str]:
    normalized_joined = normalize_geographic_text(" ".join(words))
    for signal in sorted(_COOPERATIVE_SIGNAL_WORDS, key=len, reverse=True):
        if normalized_joined.startswith(signal):
            remainder = normalized_joined[len(signal):].strip()
            if remainder:
                return " ".join(words).strip()
    return None


def _classify_hint_strength(
    *,
    refs: List[str],
    name: Optional[str],
    operator_signal: Optional[str],
    raw: str,
) -> str:
    if refs and (name or operator_signal):
        return "strong"
    if refs:
        return "strong"
    if name and len(name.split()) >= 2:
        return "moderate"
    if operator_signal:
        return "moderate"
    if name:
        return "weak"
    if raw.strip():
        return "unstructured"
    return "empty"


def derive_phase3_route_hint_contract(
    route_hint: Any,
    *,
    ref_catalog: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    raw = str(route_hint or "").strip()
    out: Dict[str, Any] = {
        "route_hint": raw or None,
        "refs": [],
        "name": None,
        "service_route_ref": None,
        "operator_signal": None,
        "hint_strength": "empty",
    }
    if not raw:
        return out

    alias_map = _build_ref_alias_map(ref_catalog)
    refs: List[str] = []
    name_parts: List[str] = []
    operator_signal: Optional[str] = None
    segments = [segment.strip() for segment in re.split(r"[,;/|\n]+", raw) if segment.strip()] or [raw]

    for segment in segments:
        # Check for corridor pattern: "PlaceA - PlaceB"
        corridor_match = _CORRIDOR_DASH_RE.match(segment)
        if corridor_match:
            left = corridor_match.group(1).strip()
            right = corridor_match.group(2).strip()
            # Corridor dashes are route names, not refs
            left_ref = _looks_like_ref_token(left) if " " not in left else None
            right_ref = _looks_like_ref_token(right) if " " not in right else None
            if left_ref and not right_ref:
                if left_ref not in refs:
                    refs.append(left_ref)
                name_parts.append(right)
            elif right_ref and not left_ref:
                if right_ref not in refs:
                    refs.append(right_ref)
                name_parts.append(left)
            else:
                name_parts.append(segment)
            continue

        whole_alias_match = alias_map.get(_normalize_ref_alias(segment))
        whole_ref_match = whole_alias_match or (
            _looks_like_ref_token(segment) if " " not in segment.strip() else None
        )
        if whole_ref_match and " " not in segment.strip():
            if whole_ref_match not in refs:
                refs.append(whole_ref_match)
            continue

        words = [word for word in re.split(r"\s+", segment) if word]

        # Check for cooperative/operator signal in this segment
        coop_signal = _extract_cooperative_signal(words)
        if coop_signal and operator_signal is None:
            operator_signal = coop_signal

        if whole_alias_match and len(words) > 1:
            refs.append(whole_alias_match) if whole_alias_match not in refs else None
        else:
            segment_refs: List[str] = []
            segment_name_words: List[str] = []
            transport_mode_only = True

            for word in words:
                if _is_transport_mode_word(word):
                    # Transport mode words are noise — skip them from name
                    continue
                transport_mode_only = False
                maybe_ref = _coerce_ref_token(word, alias_map=alias_map)
                if maybe_ref:
                    if maybe_ref not in segment_refs and maybe_ref not in refs:
                        segment_refs.append(maybe_ref)
                else:
                    segment_name_words.append(word)

            for ref in segment_refs:
                if ref not in refs:
                    refs.append(ref)

            if segment_name_words:
                name_parts.append(" ".join(segment_name_words).strip())
            elif not segment_refs and not coop_signal and not transport_mode_only:
                name_parts.append(segment)

    name = ", ".join([part for part in name_parts if part]).strip() or None
    if name and refs and normalize_geographic_text(name) in _GENERIC_ROUTE_HINT_NAMES:
        name = None

    # If cooperative signal was extracted and is the only name-like content, clear name
    if name and operator_signal and normalize_geographic_text(name) == normalize_geographic_text(operator_signal):
        name = None

    hint_strength = _classify_hint_strength(
        refs=refs,
        name=name,
        operator_signal=operator_signal,
        raw=raw,
    )

    out["refs"] = refs
    out["name"] = name
    out["service_route_ref"] = refs[0] if refs else None
    out["operator_signal"] = operator_signal
    out["hint_strength"] = hint_strength
    return out


__all__ = ["derive_phase3_route_hint_contract"]
