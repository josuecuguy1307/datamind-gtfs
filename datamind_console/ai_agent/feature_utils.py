from __future__ import annotations

from typing import Any


def flatten_numeric_features(value: Any, *, prefix: str = "") -> dict[str, float]:
    out: dict[str, float] = {}

    def _walk(v: Any, key: str) -> None:
        if isinstance(v, bool):
            out[key] = 1.0 if v else 0.0
            return
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[key] = float(v)
            return
        if isinstance(v, dict):
            for k, child in v.items():
                nk = f"{key}.{k}" if key else str(k)
                _walk(child, nk)
            return
        if isinstance(v, list):
            for i, child in enumerate(v):
                nk = f"{key}[{i}]" if key else f"[{i}]"
                _walk(child, nk)
            return

    if isinstance(value, dict):
        for k, child in value.items():
            base = f"{prefix}.{k}" if prefix else str(k)
            _walk(child, base)
    else:
        _walk(value, prefix or "value")

    return out
