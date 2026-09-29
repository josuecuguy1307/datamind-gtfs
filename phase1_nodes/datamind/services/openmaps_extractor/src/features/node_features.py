from __future__ import annotations
from typing import Any, Dict, Tuple

JsonDict = Dict[str, Any]

STOP_TAGS = [
    ("highway", "bus_stop"),
    ("public_transport", "platform"),
    ("public_transport", "stop_position"),
    ("amenity", "bus_station"),
    ("railway", "tram_stop"),
]


POI_NEG_KEYS = {"shop", "office", "tourism"}  # expand later
POI_NEG_AMENITY = {"school","hospital","restaurant","cafe","bar","bank","pharmacy","clinic"}


def heuristic_confidence(tags: JsonDict, tag_kind: str) -> float:

    conf = 0.30
    if "name" in tags: conf += 0.25
    if "ref" in tags: conf += 0.15
    if "operator" in tags: conf += 0.10
    if tag_kind == "station": conf += 0.10

    if any(k in tags for k in POI_NEG_KEYS):
        conf -= 0.25
    if tags.get("amenity") in POI_NEG_AMENITY:
        conf -=0.25
    return max(0.0, min(1.0, conf))

def baseline_stop_poi(confidence_v0: float) -> Tuple[float, float, str]:
    prob_stop = confidence_v0
    prob_poi = 1.0 - confidence_v0
    pred = "STOP" if prob_stop >= 0.5 else "POI"
    return prob_stop, prob_poi, pred


    