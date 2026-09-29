from __future__ import annotations


def validate_window(start_time: str, end_time: str, headway_secs: int | None) -> None:
    if not start_time or not end_time:
        raise ValueError("start_time and end_time are required")
    if len(start_time.split(":")) != 3 or len(end_time.split(":")) != 3:
        raise ValueError("time must be HH:MM:SS")
    if headway_secs is not None and int(headway_secs) <= 0:
        raise ValueError("headway_secs must be > 0")
