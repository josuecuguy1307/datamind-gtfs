"""
Phase 4 – Candidate generation for route naming

This module generates plausible route name candidates.
NO scoring. NO ranking. NO ML. NO persistence.
"""

from typing import Dict, List, Set


# -------------------------------------------------
# Public API
# -------------------------------------------------

def generate_candidates(evidence: Dict[str, object]) -> List[str]:
    """
    Generate a set of plausible route name candidates from semantic evidence.

    Evidence may include:
        - route_ref: str
        - endpoint_a: str
        - endpoint_b: str
        - frequent_stops: List[str]
        - known_aliases: List[str]
        - corridor_name: str
        - mode: str (bus, trolleybus, tram)
    """

    candidates: Set[str] = set()

    # Generators (recall-oriented)
    candidates |= _from_endpoints(evidence)
    candidates |= _from_route_ref(evidence)
    candidates |= _from_frequent_stops(evidence)
    candidates |= _from_corridor(evidence)
    candidates |= _from_known_aliases(evidence)

    # Hard constraints / cleanup
    cleaned = {
        _normalize(c)
        for c in candidates
        if _is_valid(c)
    }

    return sorted(cleaned)


# -------------------------------------------------
# Generators
# -------------------------------------------------

def _from_endpoints(evidence: Dict[str, object]) -> Set[str]:
    a = evidence.get("endpoint_a")
    b = evidence.get("endpoint_b")

    if not a or not b:
        return set()

    return {
        f"{a} - {b}",
        f"{b} - {a}",
        f"{a} ↔ {b}",
    }


def _from_route_ref(evidence: Dict[str, object]) -> Set[str]:
    ref = evidence.get("route_ref")
    mode = evidence.get("mode", "").lower()

    if not ref:
        return set()

    base = {ref, f"Ruta {ref}", f"Línea {ref}"}

    if mode:
        base |= {
            f"{mode.capitalize()} {ref}",
            f"{mode.capitalize()} Línea {ref}",
        }

    return base


def _from_frequent_stops(evidence: Dict[str, object]) -> Set[str]:
    stops = evidence.get("frequent_stops") or []

    out = set()
    for s in stops[:5]:  # limit for recall control
        out |= {
            s,
            f"Ruta {s}",
        }

    return out


def _from_corridor(evidence: Dict[str, object]) -> Set[str]:
    corridor = evidence.get("corridor_name")
    if not corridor:
        return set()

    return {
        corridor,
        f"Corredor {corridor}",
    }


def _from_known_aliases(evidence: Dict[str, object]) -> Set[str]:
    aliases = evidence.get("known_aliases") or []
    return set(aliases)


# -------------------------------------------------
# Validation & normalization
# -------------------------------------------------

def _normalize(name: str) -> str:
    """
    Normalize whitespace and casing.
    """
    return " ".join(name.strip().split())


def _is_valid(name: str) -> bool:
    """
    Hard constraints ONLY (no heuristics).
    """
    if not name:
        return False
    if len(name) < 3:
        return False
    if len(name) > 80:
        return False
    return True
