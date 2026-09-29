from __future__ import annotations

from importlib import import_module
from typing import Any, Dict, Tuple

_EXPORTS: Dict[str, Tuple[str, str]] = {
    "render_route_queue_table": (".route_queue_table", "render_route_queue_table"),
    "render_semantics_search_table": (".semantics_search_table", "render_semantics_search_table"),
    "render_semantics_stats_cards": (".semantics_stats_cards", "render_semantics_stats_cards"),
    "render_route_semantics_detail_card": (".route_semantics_detail_card", "render_route_semantics_detail_card"),
    "render_phase4_workspace_map": (".phase4_workspace_map", "render_phase4_workspace_map"),
    "render_naming_candidate_insights": (".naming_candidate_insights", "render_naming_candidate_insights"),
    "render_confidence_distribution_histogram": (".confidence_distribution_histogram", "render_confidence_distribution_histogram"),
}

__all__ = list(_EXPORTS.keys())


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(str(name))
    if target is None:
        raise AttributeError(name)
    module_name, attr_name = target
    module = import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
