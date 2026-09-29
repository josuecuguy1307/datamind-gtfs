"""Naming fixers — reconstruct route names, canonicalize operators, deduplicate short names.

All fixers are idempotent and return FixAttempt with confidence score.
"""
from __future__ import annotations

import json
import os
import unicodedata
from typing import Optional

from ..config import OPERATOR_CATALOG_PATH
from ..models import EntityIssue, FixAttempt, RouteSemantics


def fix_reconstruct_route_name(sem: RouteSemantics, issue: EntityIssue) -> FixAttempt:
    """Reconstruct route name from public_origin + public_destination.

    If origin/destination are available, builds "{origin} - {destination}".

    phase_origin: 4
    rule_name: route_name_garbage
    """
    origin = sem.public_origin
    dest = sem.public_destination

    if origin and dest:
        new_name = f"{origin.strip()} \u2013 {dest.strip()}"
        confidence = 0.85
        return FixAttempt(
            success=True, new_value=new_name, confidence=confidence,
            log=f"Reconstructed name for {sem.route_id[:8]}: {new_name!r}",
        )

    if origin:
        new_name = origin.strip()
        confidence = 0.5
    elif dest:
        new_name = dest.strip()
        confidence = 0.5
    else:
        return FixAttempt(
            success=False, new_value=None, confidence=0.0,
            log=f"No origin/destination available to reconstruct name for {sem.route_id[:8]}",
        )

    return FixAttempt(
        success=True, new_value=new_name, confidence=confidence,
        log=f"Partial name reconstruction for {sem.route_id[:8]}: {new_name!r} (missing {'destination' if origin else 'origin'})",
    )


def fix_canonicalize_operator(sem: RouteSemantics, issue: EntityIssue) -> FixAttempt:
    """Canonicalize operator name using operator_catalog.json.

    Confidence: 1.0 if exact match in catalog, fuzz_ratio/100 otherwise.

    phase_origin: 4
    rule_name: operator_inconsistency
    """
    canonical = sem.extra.get("canonical_operator")
    if canonical:
        return FixAttempt(
            success=True, new_value=canonical, confidence=1.0,
            log=f"Canonicalized operator for {sem.route_id[:8]}: {sem.operator!r} -> {canonical!r}",
        )

    # Try loading operator catalog
    catalog = _load_operator_catalog()
    if not catalog or not sem.operator:
        return FixAttempt(
            success=False, new_value=None, confidence=0.0,
            log=f"No catalog match for operator {sem.operator!r}",
        )

    normalized = _normalize_text(sem.operator)
    for canon_name, variants in catalog.items():
        if normalized == _normalize_text(canon_name):
            return FixAttempt(
                success=True, new_value=canon_name, confidence=1.0,
                log=f"Exact catalog match: {sem.operator!r} -> {canon_name!r}",
            )
        for v in (variants if isinstance(variants, list) else []):
            if normalized == _normalize_text(v):
                return FixAttempt(
                    success=True, new_value=canon_name, confidence=0.95,
                    log=f"Variant match: {sem.operator!r} -> {canon_name!r} (via {v!r})",
                )

    # Fuzzy fallback
    best_score = 0.0
    best_name = None
    for canon_name in catalog:
        score = _simple_ratio(normalized, _normalize_text(canon_name))
        if score > best_score:
            best_score = score
            best_name = canon_name

    if best_name and best_score > 0.7:
        return FixAttempt(
            success=True, new_value=best_name, confidence=best_score,
            log=f"Fuzzy match: {sem.operator!r} -> {best_name!r} (score={best_score:.2f})",
        )

    return FixAttempt(
        success=False, new_value=None, confidence=0.0,
        log=f"No catalog match for operator {sem.operator!r}",
    )


def fix_short_name_collision(sem: RouteSemantics, issue: EntityIssue) -> FixAttempt:
    """Deduplicate short_name by appending direction suffix.

    phase_origin: 4
    rule_name: short_name_collision
    """
    direction = sem.extra.get("direction_id", 0)
    suffix = "-A" if direction == 0 else "-B"
    new_name = f"{sem.route_short_name}{suffix}"
    return FixAttempt(
        success=True, new_value=new_name, confidence=0.7,
        log=f"Deduplicated short name: {sem.route_short_name!r} -> {new_name!r}",
    )


def _normalize_text(s: str) -> str:
    """Strip accents, lowercase, collapse whitespace."""
    nfkd = unicodedata.normalize("NFKD", s)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(stripped.lower().split())


def _simple_ratio(a: str, b: str) -> float:
    """Character-level similarity (Sørensen-Dice on bigrams)."""
    if a == b:
        return 1.0
    if len(a) < 2 or len(b) < 2:
        return 0.0
    ba = {a[i:i+2] for i in range(len(a) - 1)}
    bb = {b[i:i+2] for i in range(len(b) - 1)}
    return (2.0 * len(ba & bb)) / (len(ba) + len(bb))


_operator_catalog_cache = None

def _load_operator_catalog():
    global _operator_catalog_cache
    if _operator_catalog_cache is not None:
        return _operator_catalog_cache
    try:
        path = OPERATOR_CATALOG_PATH
        if not os.path.isabs(path):
            # Try relative to project root
            for base in [os.getcwd(), os.path.dirname(__file__)]:
                candidate = os.path.join(base, path)
                if os.path.exists(candidate):
                    path = candidate
                    break
        if os.path.exists(path):
            with open(path, "r") as f:
                _operator_catalog_cache = json.load(f)
                return _operator_catalog_cache
    except Exception:
        pass
    _operator_catalog_cache = {}
    return _operator_catalog_cache


FIXERS = {
    "route_name_garbage": fix_reconstruct_route_name,
    "short_name_collision": fix_short_name_collision,
    "operator_inconsistency": fix_canonicalize_operator,
}
