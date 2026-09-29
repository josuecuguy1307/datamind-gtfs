from __future__ import annotations


def hhmmss_to_seconds(t: str) -> int:
    h, m, s = [int(x) for x in t.split(":")]
    return h * 3600 + m * 60 + s


def seconds_to_hhmmss(v: int) -> str:
    h = v // 3600
    rem = v % 3600
    m = rem // 60
    s = rem % 60
    return f"{h:02d}:{m:02d}:{s:02d}"
