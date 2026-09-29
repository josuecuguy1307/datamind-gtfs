from __future__ import annotations
from typing import Any, Dict, Tuple

from phase1_nodes.datamind.services.openmaps_extractor.src.features.node_features import (
    heuristic_confidence,
    baseline_stop_poi,
)


def predict_from_tags(tags: Dict[str, Any], tag_kind: str) -> Tuple[float, float, str, str]:
    conf = heuristic_confidence(tags, tag_kind)
    prob_stop, prob_poi, pred = baseline_stop_poi(conf)
    return prob_stop, prob_poi, pred, "baseline_v0"
