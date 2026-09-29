from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Sequence

from src.constructor_v2.clients.valhalla_client import ValhallaClient
from src.constructor_v2.common import ensure_parent_dir, stable_hash
from src.constructor_v2.schemas.route_input import NormalizedStop


@dataclass(slots=True)
class MatrixResult:
    stop_ids: tuple[str, ...]
    distance_matrix_m: list[list[float]]
    duration_matrix_s: list[list[float]]
    engine: str
    complete: bool
    missing_pairs: list[tuple[str, str]] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def objective_matrix(self, objective: str) -> list[list[float]]:
        if objective == "distance":
            return self.distance_matrix_m
        return self.duration_matrix_s

    def to_dict(self) -> dict[str, Any]:
        return {
            "stop_ids": list(self.stop_ids),
            "distance_matrix_m": self.distance_matrix_m,
            "duration_matrix_s": self.duration_matrix_s,
            "engine": self.engine,
            "complete": self.complete,
            "missing_pairs": [list(pair) for pair in self.missing_pairs],
            "diagnostics": dict(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MatrixResult":
        return cls(
            stop_ids=tuple(payload["stop_ids"]),
            distance_matrix_m=payload["distance_matrix_m"],
            duration_matrix_s=payload["duration_matrix_s"],
            engine=payload["engine"],
            complete=bool(payload["complete"]),
            missing_pairs=[tuple(pair) for pair in payload.get("missing_pairs") or []],
            diagnostics=payload.get("diagnostics") or {},
        )


class MatrixBuilder:
    def __init__(self, client: ValhallaClient, *, cache_dir: Path | None = None) -> None:
        self.client = client
        self.cache_dir = cache_dir

    def _cache_path(self, stops: Sequence[NormalizedStop]) -> Path | None:
        if self.cache_dir is None:
            return None
        key = stable_hash(
            [
                self.client.base_url,
                self.client.costing,
                self.client.shape_format,
                [(stop.stop_id, stop.lat, stop.lon) for stop in stops],
            ]
        )
        return self.cache_dir / f"{key}.json"

    def build(self, stops: Sequence[NormalizedStop]) -> MatrixResult:
        if len(stops) < 2:
            raise ValueError("Matrix builder needs at least two stops")

        cache_path = self._cache_path(stops)
        if cache_path is not None and cache_path.exists():
            return MatrixResult.from_dict(json.loads(cache_path.read_text()))

        matrix_payload = self.client.try_sources_to_targets_matrix([(stop.lon, stop.lat) for stop in stops])
        if matrix_payload:
            result = self._from_sources_to_targets(stops, matrix_payload)
        else:
            result = self._from_pairwise_routes(stops)

        if cache_path is not None:
            ensure_parent_dir(cache_path)
            cache_path.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return result

    def _from_sources_to_targets(
        self,
        stops: Sequence[NormalizedStop],
        payload: dict[str, Any],
    ) -> MatrixResult:
        sources_to_targets = payload.get("sources_to_targets") or []
        distance_matrix: list[list[float]] = []
        duration_matrix: list[list[float]] = []
        missing_pairs: list[tuple[str, str]] = []
        for source_index, row in enumerate(sources_to_targets):
            distance_row: list[float] = []
            duration_row: list[float] = []
            for target_index, cell in enumerate(row):
                if source_index == target_index:
                    distance_row.append(0.0)
                    duration_row.append(0.0)
                    continue
                if cell is None:
                    missing_pairs.append((stops[source_index].stop_id, stops[target_index].stop_id))
                    distance_row.append(float("inf"))
                    duration_row.append(float("inf"))
                    continue
                distance_row.append(float(cell.get("distance", 0.0)) * 1000.0)
                duration_row.append(float(cell.get("time", 0.0)))
            distance_matrix.append(distance_row)
            duration_matrix.append(duration_row)
        return MatrixResult(
            stop_ids=tuple(stop.stop_id for stop in stops),
            distance_matrix_m=distance_matrix,
            duration_matrix_s=duration_matrix,
            engine="sources_to_targets",
            complete=not missing_pairs,
            missing_pairs=missing_pairs,
            diagnostics={"endpoint_snapshot": self.client.debug_snapshot(), "primary_matrix_api": True},
        )

    def _from_pairwise_routes(self, stops: Sequence[NormalizedStop]) -> MatrixResult:
        size = len(stops)
        distance_matrix = [[0.0 if i == j else float("inf") for j in range(size)] for i in range(size)]
        duration_matrix = [[0.0 if i == j else float("inf") for j in range(size)] for i in range(size)]
        missing_pairs: list[tuple[str, str]] = []
        for source_index, source in enumerate(stops):
            for target_index, target in enumerate(stops):
                if source_index == target_index:
                    continue
                try:
                    pair = self.client.pairwise_cost(
                        (source.stop_id, (source.lon, source.lat)),
                        (target.stop_id, (target.lon, target.lat)),
                    )
                except Exception:
                    missing_pairs.append((source.stop_id, target.stop_id))
                    continue
                distance_matrix[source_index][target_index] = pair.distance_m
                duration_matrix[source_index][target_index] = pair.duration_s
        return MatrixResult(
            stop_ids=tuple(stop.stop_id for stop in stops),
            distance_matrix_m=distance_matrix,
            duration_matrix_s=duration_matrix,
            engine="route_pair_fallback",
            complete=not missing_pairs,
            missing_pairs=missing_pairs,
            diagnostics={"endpoint_snapshot": self.client.debug_snapshot(), "primary_matrix_api": False},
        )
