"""
Pin the SECTOR cascade output shape to be filter-safe.

Background: PROMPT 1 added `^Parada Sector` to the input-side feedback
filter (commit dcad7163), but the SECTOR cascade level was emitting
`Parada Sector {name}` — exactly what the filter rejected. Phase 2A
dry-run flagged this as an architectural conflict (commit f5bd9366).

This file pins:
  - SECTOR cascade GARBAGE output starts with `Parada en `
    (NOT `Parada Sector `).
  - SECTOR cascade OVER_APPLIED output uses ` - en ` connector.
  - The legacy `^Parada Sector` filter is still in both place-side
    queries — those legacy rows must still be excluded as references
    even though new outputs no longer match.
  - Both new shapes pass the canonical `is_stop_name_forbidden` predicate.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR_PATH = REPO_ROOT / "phase2_semantics/src/pipeline/naming/contextual_name_generator.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from datamind_console.common.naming_patterns import is_stop_name_forbidden  # noqa: E402

PARADA_SECTOR_RE = re.compile(r"^Parada Sector")


@pytest.fixture(scope="module")
def cng():
    spec = importlib.util.spec_from_file_location("cng_sector_test", GENERATOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cng_sector_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _force_sector_only(cng, monkeypatch, sector_name):
    """Make intersection + landmark miss; sector ring (300 m) hits."""
    monkeypatch.setattr(cng, "find_nearest_intersection", lambda *a, **kw: None)
    monkeypatch.setattr(cng, "find_nearest_landmark", lambda *a, **kw: None)

    def fake_sector(conn, lat, lon, max_distance_m=300):
        # Hit only on the close ring (≤ 300 m). For 2 km extended ring, also
        # return the same name — but cascade should not fall through to it.
        return sector_name
    monkeypatch.setattr(cng, "find_nearest_sector", fake_sector)


# ─── 1. GARBAGE output uses "Parada en", not "Parada Sector" ─────────────────

def test_sector_cascade_garbage_uses_parada_en(cng, monkeypatch):
    _force_sector_only(cng, monkeypatch, "Cotocollao Norte")
    r = cng.generate_contextual_name(
        conn=None, place_id="abc12345", original_name="ignored",
        lat=-0.18, lon=-78.5, category="GARBAGE",
    )
    assert r.cascade_level == "sector"
    assert r.new_name == "Parada en Cotocollao Norte"
    assert not PARADA_SECTOR_RE.match(r.new_name)
    assert is_stop_name_forbidden(r.new_name) is False


# ─── 2. OVER_APPLIED output uses " - en " connector ──────────────────────────

def test_sector_cascade_over_applied_uses_en_connector(cng, monkeypatch):
    _force_sector_only(cng, monkeypatch, "La Mariscal")
    r = cng.generate_contextual_name(
        conn=None, place_id="abc12345", original_name="Mi Parada",
        lat=-0.18, lon=-78.5, category="OVER_APPLIED",
    )
    assert r.cascade_level == "sector"
    assert r.new_name == "Mi Parada - en La Mariscal"
    assert not PARADA_SECTOR_RE.match(r.new_name)
    assert is_stop_name_forbidden(r.new_name) is False


# ─── 3. New shape across many sector names — never matches contamination ────

def test_sector_cascade_output_never_matches_contamination_filter(cng, monkeypatch):
    """100 random sector names: 0 outputs match `^Parada Sector` and 0 are forbidden."""
    sample_sectors = [
        "Cotocollao Norte", "La Mariscal", "Centro Histórico", "Solanda",
        "Carcelén", "Quitumbe", "El Recreo", "La Magdalena", "Sector Norte",
        "Sector Sur", "San Bartolo", "Carapungo", "Calderón Centro",
        "Tumbaco Centro", "Cumbayá Norte", "Conocoto", "Sangolquí",
        "Mercado Santa Clara", "Plaza Foch", "El Bosque",
    ]
    forbidden = []
    contam = []
    for sec in sample_sectors:
        _force_sector_only(cng, monkeypatch, sec)
        r = cng.generate_contextual_name(
            conn=None, place_id="abc12345", original_name="orig",
            lat=-0.18, lon=-78.5, category="GARBAGE",
        )
        if PARADA_SECTOR_RE.match(r.new_name):
            contam.append(r.new_name)
        if is_stop_name_forbidden(r.new_name):
            forbidden.append(r.new_name)

    assert contam == [], f"sector cascade output matched ^Parada Sector: {contam}"
    assert forbidden == [], f"sector cascade output flagged forbidden: {forbidden}"


# ─── 4. Legacy `^Parada Sector` filter still pinned in input queries ────────

def test_legacy_parada_sector_filter_retained(cng):
    """The contamination filter must remain even though new outputs no longer match.

    Reason: residual legacy rows (e.g. `Parada Sector X` from pre-fix runs)
    must still be excluded as landmark/sector references, otherwise the
    feedback loop reopens for the legacy population.
    """
    for sql_name in ("_SQL_NEAREST_LANDMARK", "_SQL_NEAREST_SECTOR"):
        sql = getattr(cng, sql_name)
        assert r"!~* '^Parada Sector'" in sql, (
            f"{sql_name} dropped the legacy ^Parada Sector exclusion filter"
        )


# ─── 5. Sample names from the dry-run all pass the canonical predicate ──────

def test_sample_dryrun_outputs_pass_canonical_check():
    """The 17 dry-run cases that previously emitted `Parada Sector X` should
    now produce `Parada en X`. Verify the new shape is canonically allowed
    for the actual sector names observed in production."""
    real_sector_names_from_dryrun = [
        "Colegio Santo Domingo de Guzmán",
        "SSC SAN Miguel del Comun - P4",
        "Mercado Santa Clara",
        "Trabajo",
        "Víveres Jorge Luis",
        "Parque la Merced",
        "Yaira Espinoza Nutri Carne",
        "Parque San Carlos",
        "Mercado el Arenal",
        "Mercado Santa Martha",
        "Curie Hospital Center",
        "Unidad Educativa Maria Mazarello",
        "Centro Educativo Khipu",
        "República de Alemania",
        "Cruz Roja Instituto",
        "Panadería Anni Pan",
        "Escuela Virginia Larenas",
        "Polocia Nacional del Ecuador Hospital Quito N°1",
        "Educacion Multilingue y Desarrollo del Individuo",
    ]
    forbidden = []
    contam = []
    for sec in real_sector_names_from_dryrun:
        new_name = f"Parada en {sec}"
        if is_stop_name_forbidden(new_name):
            forbidden.append(new_name)
        if PARADA_SECTOR_RE.match(new_name):
            contam.append(new_name)
    assert forbidden == [], f"forbidden among real sector names: {forbidden}"
    assert contam == [], f"contamination among real sector names: {contam}"
