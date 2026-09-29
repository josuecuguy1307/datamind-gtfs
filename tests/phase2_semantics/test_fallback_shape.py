"""
Output-side fallback shape tests for contextual_name_generator.

Background: this generator (Module A) is the DB-only naming cascade. It
runs intersection 60m → landmark 75m → sector 300m → extended_sector 2km.
When all four miss it returns level="orphan". The orphan branch was
previously emitting "Parada Aislada" — a string that the canonical
forbidden-pattern set later marked as garbage, creating a self-blocking
loop in the pre-export enforcer.

Current contract (since the rural extended cascade landed):

  - `Parada Cerca de {far_sector}` when the 2 km ring hits.
  - GARBAGE orphan returns an EMPTY new_name (cascade_level="orphan").
    Callers that route through stop_treater escalate to the rural extended
    cascade in datamind_console.common.extended_stop_naming.
  - OVER_APPLIED orphan preserves the original name (no decoration).

The string "Parada Aislada" is retired from this module entirely.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR_PATH = REPO_ROOT / "phase2_semantics/src/pipeline/naming/contextual_name_generator.py"

# Make datamind_console.common importable for the canonical forbidden predicate.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from datamind_console.common.naming_patterns import is_stop_name_forbidden  # noqa: E402


HASH_RE = re.compile(r"\([0-9a-f]{6,8}\)")
PARADA_PAREN_RE = re.compile(r"^Parada\s*\(")
PARADA_SECTOR_RE = re.compile(r"^Parada Sector")


@pytest.fixture(scope="module")
def cng():
    spec = importlib.util.spec_from_file_location("cng_fallback_test", GENERATOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cng_fallback_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _no_rings(cng, monkeypatch, *, sector_at_2km=None):
    """All rings empty by default; optional far-sector hit at 2 km."""
    monkeypatch.setattr(cng, "find_nearest_intersection", lambda *a, **kw: None)
    monkeypatch.setattr(cng, "find_nearest_landmark", lambda *a, **kw: None)

    sector_calls = []

    def fake_sector(conn, lat, lon, max_distance_m=300):
        sector_calls.append(max_distance_m)
        # The 300 m ring (cascade step 3) misses; the 2000 m ring (step 4)
        # may or may not hit depending on the test setup.
        if max_distance_m >= 1000:
            return sector_at_2km
        return None

    monkeypatch.setattr(cng, "find_nearest_sector", fake_sector)
    return sector_calls


# ─── 1. No hash pattern ever emitted ──────────────────────────────────────────

def test_fallback_no_hash_pattern(cng, monkeypatch):
    """Every fallback path must emit a name free of `(hexhash)`."""
    # Case A: extended sector hits.
    _no_rings(cng, monkeypatch, sector_at_2km="Cotocollao Norte")
    r = cng.generate_contextual_name(
        conn=None, place_id="abcdef12345", original_name="ignored",
        lat=-0.18, lon=-78.5, category="GARBAGE",
    )
    assert HASH_RE.search(r.new_name) is None, f"hash leaked: {r.new_name!r}"

    # Case B: true orphan.
    _no_rings(cng, monkeypatch, sector_at_2km=None)
    r = cng.generate_contextual_name(
        conn=None, place_id="abcdef12345", original_name="ignored",
        lat=-0.18, lon=-78.5, category="GARBAGE",
    )
    assert HASH_RE.search(r.new_name) is None, f"hash leaked: {r.new_name!r}"


# ─── 2. Far-sector path used when available ───────────────────────────────────

def test_fallback_far_sector_used_when_available(cng, monkeypatch):
    """When the 300 m ring misses but the 2 km ring hits, use it."""
    _no_rings(cng, monkeypatch, sector_at_2km="La Magdalena")
    r = cng.generate_contextual_name(
        conn=None, place_id="abcdef12345", original_name="orig",
        lat=-0.18, lon=-78.5, category="GARBAGE",
    )
    assert "Cerca de La Magdalena" in r.new_name
    assert r.cascade_level == "extended_sector"
    assert is_stop_name_forbidden(r.new_name) is False


# ─── 3. Orphan path when nothing within 2 km ──────────────────────────────────

def test_fallback_orphan_returns_empty_name(cng, monkeypatch):
    """GARBAGE orphan must return an empty new_name (signal to escalate).

    The string "Parada Aislada" is retired — it conflicts with the
    canonical STOP_FORBIDDEN_PATTERNS set. Callers (stop_treater) escalate
    to the rural extended cascade in extended_stop_naming.
    """
    _no_rings(cng, monkeypatch, sector_at_2km=None)
    r = cng.generate_contextual_name(
        conn=None, place_id="abcdef12345", original_name="ignored",
        lat=-0.18, lon=-78.5, category="GARBAGE",
    )
    assert r.new_name == ""
    assert r.cascade_level == "orphan"
    assert r.confidence == 0.0


# ─── 4. All fallback shapes pass canonical forbidden predicate ────────────────

def test_fallback_passes_canonical_forbidden_check(cng, monkeypatch):
    """100 random orphan-like coords across Quito metro: 0 forbidden outputs."""
    import random
    random.seed(42)

    far_sector_pool = [None, "Cotocollao", "La Magdalena", "El Recreo",
                       "Solanda", "Carcelén", "Quitumbe", "La Mariscal"]

    forbidden_outputs: list[str] = []
    new_contam_outputs: list[str] = []

    for _ in range(100):
        lat = -0.5 + random.random() * 0.6   # roughly Quito metro range
        lon = -78.7 + random.random() * 0.4
        sector_at_2km = random.choice(far_sector_pool)

        _no_rings(cng, monkeypatch, sector_at_2km=sector_at_2km)
        r = cng.generate_contextual_name(
            conn=None, place_id="abcdef12345", original_name="orig",
            lat=lat, lon=lon, category="GARBAGE",
        )
        # Orphan now returns empty new_name (signal to escalate); empty
        # strings ARE forbidden by `^\s*$` but are not "outputs" — they
        # are sentinel values for the caller's escalation. Skip them here.
        if r.cascade_level == "orphan" and r.new_name == "":
            continue
        if is_stop_name_forbidden(r.new_name):
            forbidden_outputs.append(r.new_name)
        if (HASH_RE.search(r.new_name)
                or PARADA_PAREN_RE.search(r.new_name)
                or PARADA_SECTOR_RE.search(r.new_name)):
            new_contam_outputs.append(r.new_name)

    assert forbidden_outputs == [], (
        f"{len(forbidden_outputs)}/100 fallback outputs flagged forbidden: "
        f"{forbidden_outputs[:5]}"
    )
    assert new_contam_outputs == [], (
        f"{len(new_contam_outputs)}/100 fallback outputs match a "
        f"contamination pattern: {new_contam_outputs[:5]}"
    )


# ─── 5. Old hash fallback shape never emitted ─────────────────────────────────

def test_old_hash_fallback_no_longer_emitted(cng, monkeypatch):
    """Force every cascade-miss path; verify zero `Parada (8hex)` outputs."""
    OLD_SHAPE = re.compile(r"^Parada\s*\([0-9a-f]{6,8}\)$")

    samples = []
    for category in ("GARBAGE", "OVER_APPLIED"):
        # Path A: extended sector hits.
        _no_rings(cng, monkeypatch, sector_at_2km="SectorX")
        samples.append(cng.generate_contextual_name(
            conn=None, place_id="deadbeef1234", original_name="orig",
            lat=-0.2, lon=-78.5, category=category,
        ).new_name)
        # Path B: true orphan.
        _no_rings(cng, monkeypatch, sector_at_2km=None)
        samples.append(cng.generate_contextual_name(
            conn=None, place_id="deadbeef1234", original_name="orig",
            lat=-0.2, lon=-78.5, category=category,
        ).new_name)

    bad = [s for s in samples if OLD_SHAPE.match(s)]
    assert bad == [], f"old hash fallback shape leaked: {bad}"


# ─── 6. OVER_APPLIED orphan keeps original (no hash appended) ─────────────────

def test_over_applied_orphan_preserves_original(cng, monkeypatch):
    """Pre-fix, OVER_APPLIED orphan emitted `{orig} (hash)`. Now: keeps orig."""
    _no_rings(cng, monkeypatch, sector_at_2km=None)
    r = cng.generate_contextual_name(
        conn=None, place_id="abcdef12345", original_name="Mi Parada",
        lat=-0.18, lon=-78.5, category="OVER_APPLIED",
    )
    assert r.new_name == "Mi Parada"
    assert r.cascade_level == "orphan"
    assert HASH_RE.search(r.new_name) is None
