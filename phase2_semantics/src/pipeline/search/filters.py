from __future__ import annotations
from typing import Dict, Iterable, List, Callable




# -----------------------------
# Basic filter predicates
# -----------------------------

def filter_by_kind(doc: Dict, *, allowed: Iterable[str]) -> bool:
    """
    Filter by object kind.
    Example: stop / poi
    """

    return doc.get("kind") in allowed


def filter_by_region(doc: Dict, *, region: str | None) -> bool:
    """
    Filter by region (city, metro area, etc).
    """
    if region is None:
        return True
    return doc.get("region") == region




def filter_by_transport(doc: Dict, *, modes: Iterable[str] | None) -> bool:
    """
    Filter by transport modes.
    Example: bus, metro, tram
    """
    if not modes:
        return True

    doc_modes = set(doc.get("transport_modes", []))
    return bool(doc_modes.intersection(modes))


def filter_active(doc: Dict) -> bool:
    """
    Filter out inactive / deprecated objects.
    """
    return doc.get("active", True) is True


def apply_filters(
    docs: Iterable[Dict],
    filters: List[Callable[[Dict], bool]],

) -> List[Dict]:
    """
    Apply a list of filters (AND logic).
    """
    results: List[Dict] = []

    for d in docs:
        if all(f(d) for f in filters):
            results.append(d)
    return results

