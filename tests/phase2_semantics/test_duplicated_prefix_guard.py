"""
Issue #21 hardening — duplicated-proximity-prefix shape.

Background: 2026-04-27 bulk rename produced 5/42 auto-applied rows like
`Frente a Terminal Quitumbe - Frente a Terminal Quitumbe`. The landmark
cascade prepended `Frente a` to a candidate whose own canonical_name
already started with `Frente a` from a prior run.

Three guards were added:

  Layer 1: live-DB UPDATEs collapsed the 5 rows to a single prefix shape.
  Layer 2: `_SQL_NEAREST_LANDMARK` and `_SQL_NEAREST_SECTOR` reject
           candidates starting with `(Frente a|Junto a|Cerca de) ` so
           the cascade cannot re-prepend going forward.
  Layer 3: `STOP_FORBIDDEN_PATTERNS` flags any name with a duplicated
           proximity prefix as canonically forbidden — defensive
           backstop at every gate that calls `is_stop_name_forbidden`.

This file pins layers 2 + 3.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR_PATH = REPO_ROOT / "phase2_semantics/src/pipeline/naming/contextual_name_generator.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from datamind_console.common.naming_patterns import is_stop_name_forbidden  # noqa: E402


@pytest.fixture(scope="module")
def cng():
    spec = importlib.util.spec_from_file_location("cng_dup_test", GENERATOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cng_dup_test"] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── Layer 3: forbidden-pattern catches duplicated proximity prefix ──────────

@pytest.mark.parametrize("name", [
    "Frente a Terminal Quitumbe - Frente a Terminal Quitumbe",
    "Frente a Hospital Eugenio Espejo - Frente a Hospital Eugenio Espejo",
    "Frente a Terminal - Frente a Terminal",
    "Frente a Terminal Rio Coca - Frente a Terminal Rio Coca",
    "Frente a Terminal Quitumbe - Frente a Terminal Terrestre Quitumbe",
    "Junto a Plaza X - Junto a Plaza Y",
    "Cerca de Hospital Z - Cerca de Otro Hospital",
    "frente a x - frente a y",   # case-insensitive
])
def test_duplicated_proximity_prefix_is_forbidden(name):
    assert is_stop_name_forbidden(name), f"should be forbidden: {name!r}"


@pytest.mark.parametrize("name", [
    "Frente a Terminal Quitumbe",
    "Frente a Terminal Terrestre Quitumbe",
    "Frente a Hospital Eugenio Espejo",
    "Frente a Terminal",
    "Frente a Terminal Río Coca",
    "Junto a Museo Arquelogico Weilbauer",
    "Cerca de Museo",
    "Parada en Cotocollao Norte",
])
def test_single_proximity_prefix_passes(name):
    """Names with the prefix only ONCE must remain canonically allowed."""
    assert not is_stop_name_forbidden(name), f"should not be forbidden: {name!r}"


# ─── Layer 2: input-side filters exclude already-prefixed candidates ─────────

def test_landmark_query_excludes_already_prefixed(cng):
    """Without this filter the landmark cascade can re-prepend a proximity prefix."""
    sql = cng._SQL_NEAREST_LANDMARK
    assert r"!~* '^(Frente a|Junto a|Cerca de) '" in sql, (
        "_SQL_NEAREST_LANDMARK missing already-prefixed exclusion"
    )


def test_sector_query_excludes_already_prefixed(cng):
    sql = cng._SQL_NEAREST_SECTOR
    assert r"!~* '^(Frente a|Junto a|Cerca de) '" in sql, (
        "_SQL_NEAREST_SECTOR missing already-prefixed exclusion"
    )
