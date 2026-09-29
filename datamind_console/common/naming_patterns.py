"""Single source of truth for all forbidden name patterns.

Referenced by: Quality Gate (stop_rules, naming_rules), Pre-Export Enforcer,
Backfill Executor, Pre-Deploy Enforcer, Name Normalizer.

Do NOT duplicate these patterns elsewhere. Import from here.
See workspace/skills/hades-naming-standard/SKILL.md for documentation.
"""
from __future__ import annotations

import re
from typing import Optional

MIN_NAME_LENGTH = 3


# --- Stop name patterns (node_prod.nodes, GTFS stops.txt) --------------------
STOP_FORBIDDEN_PATTERNS = [
    re.compile(r'^\s*$'),
    re.compile(r'(?i)parada sin nombre'),
    re.compile(r'^Bus Stop \d+$', re.IGNORECASE),
    re.compile(r'(?i)^Stop [a-f0-9]{8}'),
    re.compile(r'^\d+$'),
    re.compile(r'(?i)^unnamed'),
    re.compile(r'(?i)^unknown'),
    re.compile(r'(?i)^Parada\s*$'),
    re.compile(r'(?i)^Parada\s*\('),
    re.compile(r'(?i)^Parada\s*-\d+\.'),
    re.compile(r'(?i)^Parada [a-f0-9]{8}'),
    re.compile(r'(?i)^Parada\s+\d+\s*$'),  # "Parada 1", "Parada 159" placeholders
    re.compile(r'(?i)\bparada aislada\b'),  # "Parada Aislada" — documented gap
    re.compile(r'-?\d+\.\d{4,}\s*,\s*-?\d+\.\d{4,}'),
    re.compile(r'(?i)^sin nombre'),
    re.compile(r'(?i)rel \d{5,}'),
    re.compile(r'(?i)relation \d+'),
    re.compile(r'(?i)^(way|node) \d+'),
    re.compile(r'(?i)^\[pending research\]'),
    re.compile(r'(?i)^desconocido'),
    re.compile(r'^[a-f0-9]{8}'),
    re.compile(r'(?i)(Frente a|Junto a|Cerca de).+\1'),
]


# --- Route name patterns (route_prod, GTFS routes.txt) ----------------------
ROUTE_FORBIDDEN_PATTERNS = [
    re.compile(r'^\s*$'),
    re.compile(r'^\d+$'),
    re.compile(r'^Ruta \d+$', re.IGNORECASE),
    re.compile(r'^[a-f0-9-]{36}$', re.IGNORECASE),
    re.compile(r'^Route \d+$', re.IGNORECASE),
    re.compile(r'(?i)^unknown'),
    re.compile(r'(?i)^unnamed'),
    re.compile(r'(?i)rel \d{5,}'),
    re.compile(r'(?i)relation \d+'),
    re.compile(r'(?i)^way \d+'),
    re.compile(r'(?i)^node \d+'),
    re.compile(r'(?i)\[pending research\]'),
]


# --- Operator/agency name patterns (GTFS agency.txt) ------------------------
OPERATOR_FORBIDDEN_PATTERNS = [
    re.compile(r'^\s*$'),
    re.compile(r'(?i)^unknown'),
    re.compile(r'(?i)^unnamed'),
    re.compile(r'(?i)^desconocido'),
    re.compile(r'(?i)rel \d{5,}'),
    re.compile(r'(?i)relation \d+'),
    re.compile(r'^\d+$'),
    re.compile(r'^[a-f0-9-]{36}$', re.IGNORECASE),
    re.compile(r'(?i)^\[pending research\]'),
    re.compile(r'(?i)^cooperativa$'),
    re.compile(r'(?i)^operadora$'),
]


def is_stop_name_forbidden(name: Optional[str]) -> bool:
    if not name or len(name.strip()) < MIN_NAME_LENGTH:
        return True
    return any(p.search(name.strip()) for p in STOP_FORBIDDEN_PATTERNS)


def is_route_name_forbidden(name: Optional[str]) -> bool:
    if not name or len(name.strip()) < MIN_NAME_LENGTH:
        return True
    return any(p.search(name.strip()) for p in ROUTE_FORBIDDEN_PATTERNS)


def is_operator_name_forbidden(name: Optional[str]) -> bool:
    if not name or len(name.strip()) < MIN_NAME_LENGTH:
        return True
    return any(p.search(name.strip()) for p in OPERATOR_FORBIDDEN_PATTERNS)
