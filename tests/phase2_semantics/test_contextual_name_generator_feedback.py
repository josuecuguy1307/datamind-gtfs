"""
Feedback-loop guards for phase2_semantics.contextual_name_generator.

Background: contextual_name_generator's cascade reads the nearest landmark and
sector candidates from `geo_prod.places`. Before 2026-04-27, those queries had
no contamination filter, so a previous run's hash-suffixed fallback names
(e.g. "Parada (a1b2c3d4)", "Parada Sector X", "Foo (deadbeef)") could be picked
up as the "landmark" or "sector" reference for the next run — a self-reinforcing
contamination loop. The fix:

  1. Read from the deprecation-aware view `geo_prod.v_active_places` instead of
     `geo_prod.places` directly.
  2. Reject candidates whose `canonical_name` matches any of the three known
     contamination shapes:
       - hash suffix:    `\\([0-9a-f]{6,8}\\)`
       - "Parada (...)"  parenthesized fallback
       - "Parada Sector ..." sector fallback

These tests pin those guarantees by inspecting the SQL constants. The
intersection query reads OSM (`node_raw.overpass_elements`), not places, so it
is naturally immune; the test for it is a structural pin (it must NOT read
geo_prod.places).
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR_PATH = REPO_ROOT / "phase2_semantics/src/pipeline/naming/contextual_name_generator.py"


@pytest.fixture(scope="module")
def cng():
    """Load the generator module by file path (its package __init__ has unrelated imports)."""
    spec = importlib.util.spec_from_file_location("cng_under_test", GENERATOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cng_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── Filter pin tests ─────────────────────────────────────────────────────────

HASH_FILTER = r"!~ '\([0-9a-f]{6,8}\)'"
PARADA_PAREN_FILTER = r"!~* '^Parada\s*\('"
PARADA_SECTOR_FILTER = r"!~* '^Parada Sector'"


def test_landmark_query_excludes_contaminated_hash(cng):
    """Landmark lookup must filter out hash-suffixed names from the previous run."""
    sql = cng._SQL_NEAREST_LANDMARK
    assert HASH_FILTER in sql, "landmark query missing hash-suffix exclusion filter"
    assert PARADA_PAREN_FILTER in sql, "landmark query missing 'Parada (...)' exclusion filter"
    assert PARADA_SECTOR_FILTER in sql, "landmark query missing 'Parada Sector' exclusion filter"


def test_sector_query_excludes_parada_paren(cng):
    """Sector lookup must filter out all three contamination shapes."""
    sql = cng._SQL_NEAREST_SECTOR
    assert HASH_FILTER in sql, "sector query missing hash-suffix exclusion filter"
    assert PARADA_PAREN_FILTER in sql, "sector query missing 'Parada (...)' exclusion filter"
    assert PARADA_SECTOR_FILTER in sql, "sector query missing 'Parada Sector' exclusion filter"


def test_intersection_query_does_not_read_places(cng):
    """
    Intersection lookup reads OSM ways, not geo_prod.places, so it is naturally
    immune to feedback contamination. Pin that boundary: if anyone refactors
    intersection to consult geo_prod.places, the same hash filters MUST be
    added before this test is loosened.
    """
    sql = cng._SQL_NEAREST_INTERSECTION
    assert "geo_prod.places" not in sql, (
        "intersection query now reads geo_prod.places — add contamination filters or revert"
    )
    assert "geo_prod.v_active_places" not in sql, (
        "intersection query now reads geo_prod.v_active_places — add contamination filters or revert"
    )
    assert "node_raw.overpass_elements" in sql, "intersection query should read OSM"


# ─── View migration pin ───────────────────────────────────────────────────────

def test_v_active_places_used_not_places_directly(cng):
    """Both place-side queries must read v_active_places, not the base table."""
    for name in ("_SQL_NEAREST_LANDMARK", "_SQL_NEAREST_SECTOR"):
        sql = getattr(cng, name)
        assert "geo_prod.v_active_places" in sql, f"{name} not migrated to v_active_places"
        # Must not read the base table (which exposes deprecated rows).
        # Match `geo_prod.places` followed by whitespace / EOL — but not
        # `geo_prod.places_pre_dedup_*` snapshot tables.
        bad = re.search(r"geo_prod\.places(?![_a-zA-Z0-9])", sql)
        assert bad is None, f"{name} still reads geo_prod.places base table"


# ─── No SyntaxWarning on import ───────────────────────────────────────────────

def test_module_loads_without_syntax_warning():
    """The SQL constants must use raw strings — `\\(` is an invalid escape otherwise."""
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("error", SyntaxWarning)
        spec = importlib.util.spec_from_file_location("cng_warn_check", GENERATOR_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["cng_warn_check"] = mod
        spec.loader.exec_module(mod)
