from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RouteScheduleProfile:
    profile_id: str
    route_id: str
    direction_id: int
    service_name: str
    runtime_secs: int
    dwell_secs: int
    is_active: bool


@dataclass(frozen=True)
class ServiceWindow:
    window_id: str
    profile_id: str
    start_time: str
    end_time: str
    headway_secs: int | None
