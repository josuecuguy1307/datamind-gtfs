"""UI code must use the canonical DB helpers, not raw psycopg2 connections.

Views and widgets render on every Streamlit rerun. Raw connections there
leaked (``with psycopg2.connect() as conn`` does not close) or, cached
with ``st.cache_resource``, stayed "idle in transaction" and blocked DDL.
See docs/DB_ACCESS.md.
"""
from __future__ import annotations

import ast
from pathlib import Path

UI_ROOTS = (Path("datamind_console/views"), Path("datamind_console/widgets"), Path("datamind_console/ui"))


def _raw_connect_calls(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "connect":
            target = node.func.value
            if isinstance(target, ast.Name) and target.id == "psycopg2":
                lines.append(node.lineno)
    return lines


def test_ui_code_never_opens_raw_psycopg2_connections() -> None:
    offenders: list[str] = []
    for root in UI_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if "/tests/" in str(path):
                continue
            for line in _raw_connect_calls(path):
                offenders.append(f"{path}:{line}")
    assert not offenders, "use datamind_console.db.db.query_all/read_only_connection instead:\n" + "\n".join(offenders)


def test_ui_code_never_caches_connections() -> None:
    offenders: list[str] = []
    for root in UI_ROOTS:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                decorated = any("cache_resource" in ast.unparse(d) for d in node.decorator_list)
                if decorated and ("connect" in ast.unparse(node) or "db_conn" in ast.unparse(node)):
                    offenders.append(f"{path}:{node.lineno} {node.name}")
    assert not offenders, "never cache DB connections across reruns:\n" + "\n".join(offenders)
