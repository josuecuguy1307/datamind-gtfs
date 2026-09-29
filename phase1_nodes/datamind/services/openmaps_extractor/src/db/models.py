from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Optional, Dict, List
from uuid import UUID


JsonDict = Dict[str, Any]


@dataclass(frozen=True)
class OverpassAction:
    action_id: str
    template_path: str
    default_params: JsonDict
    outputs: List[str]


@dataclass(frozen=True)
class OverpassQuery:
    query_id: UUID
    action_id: str
    query_text: str
    params: JsonDict


@dataclass(frozen=True)
class OverpassRun:
    run_id: UUID
    query_id: UUID
    status: str                 # ok|timeout|error
    runtime_ms: Optional[int]
    element_count: Optional[int]
    response_bytes: Optional[int]
    bbox: Optional[JsonDict]    # {"south":..,"west":..,"north":..,"east":..}
    area_id: Optional[str]


@dataclass(frozen=True)
class OverpassElement:
    run_id: UUID
    osm_type: str               # node|way|relation
    osm_id: int
    lat: Optional[float]
    lon: Optional[float]
    center_lat: Optional[float]
    center_lon: Optional[float]
    tags: JsonDict
