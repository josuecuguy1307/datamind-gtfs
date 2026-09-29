from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


def _catalog_path(name: str) -> Path:
    return Path(__file__).resolve().parents[2] / "catalogs" / name


def load_json_catalog(name: str) -> Dict[str, Any]:
    p = _catalog_path(name)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def normalize_whitespace(text: str) -> str:
    return " ".join((text or "").split())


def cleanup_text(text: str) -> str:
    value = normalize_whitespace(text)
    if not value:
        return ""

    cfg = load_json_catalog("token_cleanup.json")
    repl = cfg.get("replacements") or {}
    for old, new in repl.items():
        value = value.replace(str(old), str(new))

    strip_tokens = [str(x).strip().lower() for x in (cfg.get("strip_tokens") or []) if str(x).strip()]
    parts = [p for p in value.split(" ") if p.strip()]
    parts = [p for p in parts if p.lower() not in strip_tokens]

    value = " ".join(parts)
    value = normalize_whitespace(value)
    return value.strip(" -")


def canonicalize_operator(operator_name: str) -> str:
    value = cleanup_text(operator_name)
    if not value:
        return ""

    cfg = load_json_catalog("operator_catalog.json")
    ops = cfg.get("operators") or []
    lower = value.lower()
    for row in ops:
        alias = str(row.get("alias") or "").strip().lower()
        if alias and alias == lower:
            return cleanup_text(str(row.get("canonical") or value))
    return value
