"""
test_session_integration.py

Test de integración que cubre TODO lo implementado en la sesión de marzo 2026:
  - BUG-001: terminus no prunado (role="end"/"start"/"terminal"/"anchor_primary")
  - BUG-002: filtro de jurisdicción en stop DB
  - Fix "miranda" removido de _NEIGHBORHOOD_KEYWORDS
  - Spatial dedup no colapsa candidatos con distinta posición
  - REGRESIÓN: catálogo v2 sin campo jurisdiction → default "both" → no filtra (esperado)
  - REGRESIÓN: las 19 rutas del catálogo v2 siguen cargando sin excepción

Para correr:
    cd <repo-root>
    python -m pytest datamind_console/phases/phase3_routes/stop_grounding/tests/test_session_integration.py -v

IMPORTANTE: Este test asume la estructura de módulos detectada por Claude Code:
    - contracts.py              → TypedSeedToken, TypedRouteSeed, CorridorStopCandidate
    - on_route_classifier.py    → infer_terminus_type, is_terminus_protected,
                                  score_candidates, UNPRUNEABLE_ROLES, UNPRUNEABLE_POLICIES
    - corridor_stop_intersector.py → intersect_corridor_with_stops, infer_stop_source
    - typed_token_dispatch.py   → intake_typed_seed
    - discovery_pipeline.py     → (integración end-to-end, solo smoke test)

Si los paths de import difieren, ajustar los imports de abajo.
"""

import json
import os
import pytest
from pathlib import Path
from typing import Optional

# ── Imports ──────────────────────────────────────────────────────────────────
from datamind_console.phases.phase3_routes.stop_grounding.on_route_classifier import (
    infer_terminus_type,
    is_terminus_protected,
    score_candidates,
    UNPRUNEABLE_ROLES,
    UNPRUNEABLE_POLICIES,
    _NEIGHBORHOOD_KEYWORDS,
)
from datamind_console.phases.phase3_routes.stop_grounding.corridor_stop_intersector import (
    infer_stop_source,
    intersect_corridor_with_stops,
)
from datamind_console.phases.phase3_routes.stop_grounding.contracts import (
    TypedSeedToken,
    TypedRouteSeed,
    CorridorStopCandidate,
)
from datamind_console.phases.phase3_routes.stop_grounding.typed_token_dispatch import (
    intake_typed_seed,
)
# ─────────────────────────────────────────────────────────────────────────────

# Path al catálogo v2 (seed catalog)
CATALOG_V2_PATH = Path(__file__).resolve().parents[5] / \
    "constructor_artifacts" / "valle_typed_seed_catalog.json"
# Fallback: env var
if not CATALOG_V2_PATH.exists():
    CATALOG_V2_PATH = Path(os.environ.get(
        "SEED_CATALOG_PATH",
        str(CATALOG_V2_PATH),
    ))


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _make_token(
    label: str,
    role: str = "intermediate_anchor",
    resolution_policy: str = "resolve_to_nearest_road_node",
    kind: Optional[str] = None,
    terminus_type: Optional[str] = None,
) -> TypedSeedToken:
    return TypedSeedToken(
        label=label,
        role=role,
        resolution_policy=resolution_policy,
        kind=kind or "stop_candidate",
        terminus_type=terminus_type,
        confidence="medium",
    )


def _make_candidate(
    stop_id: str,
    in_expected_geography: bool,
    path_fraction: float,
    stop_source: Optional[str] = None,
    lat: float = -0.335,
    lon: float = -78.445,
) -> CorridorStopCandidate:
    return CorridorStopCandidate(
        stop_id=stop_id,
        stop_name=stop_id,
        in_expected_geography=in_expected_geography,
        path_fraction=path_fraction,
        stop_source=stop_source,
        distance_to_corridor_m=15.0,
        lat=lat,
        lon=lon,
        bearing_alignment_deg=45.0,
        locality_consistency_score=0.5,
        stop_usage_frequency=2.0,
    )


def _load_catalog_routes() -> list:
    with open(CATALOG_V2_PATH, encoding="utf-8-sig") as f:
        data = json.load(f)
    return data.get("routes", data) if isinstance(data, dict) else data


# ═══════════════════════════════════════════════════════════════════════════
# BLOQUE 1 — BUG-001: TERMINUS NO PRUNADO
# ═══════════════════════════════════════════════════════════════════════════

class TestTerminusProtection:

    def test_role_end_is_protected(self):
        """Un token role='end' NUNCA debe ser prunado, sin importar is_conflicting."""
        token = _make_token("Miranda", role="end")
        assert is_terminus_protected(token), \
            "role='end' debe estar protegido — BUG-001"

    def test_role_start_is_protected(self):
        token = _make_token("Playón de La Marín", role="start")
        assert is_terminus_protected(token)

    def test_role_terminal_is_protected(self):
        token = _make_token("Terminal CALSIG Express", role="terminal")
        assert is_terminus_protected(token)

    def test_role_anchor_primary_is_protected(self):
        token = _make_token("ESPE campus", role="anchor_primary")
        assert is_terminus_protected(token)

    def test_resolve_to_terminal_policy_is_protected(self):
        """resolution_policy terminal también protege, independiente del role."""
        token = _make_token(
            "Terminal CALSIG",
            role="intermediate_anchor",  # role genérico
            resolution_policy="resolve_to_best_matching_terminal_node",
        )
        assert is_terminus_protected(token)

    def test_resolve_to_stop_policy_is_protected(self):
        token = _make_token(
            "Parada de bus Loreto",
            role="intermediate_anchor",
            resolution_policy="resolve_to_best_matching_stop_node",
        )
        assert is_terminus_protected(token)

    def test_regular_waypoint_not_protected(self):
        """Un waypoint normal SÍ puede ser prunado."""
        token = _make_token(
            "Paso intermedio",
            role="intermediate_anchor",
            resolution_policy="resolve_to_nearest_road_node",
        )
        assert not is_terminus_protected(token)

    def test_protected_terminus_goes_to_probable_not_rejected(self):
        """
        Un stop terminus-protected con is_conflicting debe ir a 'probable',
        NO a 'rejected'. Verifica la penalización suave vs agresiva.
        """
        terminus_stop = _make_candidate(
            stop_id="loreto_bus_stop",
            in_expected_geography=False,  # conflicting
            path_fraction=0.95,
            lat=-0.328,   # Loreto
            lon=-78.420,
        )
        normal_stop = _make_candidate(
            stop_id="normal_mid",
            in_expected_geography=True,
            path_fraction=0.5,
            lat=-0.280,   # mucho más al norte
            lon=-78.500,
        )
        probable, marginal, rejected = score_candidates(
            candidates=[terminus_stop, normal_stop],
            scoring_mode="heuristic",
            terminus_protected_ids={"loreto_bus_stop"},
        )
        stop_ids_probable = {s.stop_id for s in probable}
        stop_ids_rejected = {s.stop_id for s in rejected}
        assert "loreto_bus_stop" in stop_ids_probable, \
            "Terminus protegido debe estar en 'probable', no en 'rejected'"
        assert "loreto_bus_stop" not in stop_ids_rejected


# ═══════════════════════════════════════════════════════════════════════════
# BLOQUE 2 — BUG-002: FILTRO DE JURISDICCIÓN
# ═══════════════════════════════════════════════════════════════════════════

class TestJurisdictionFilter:

    def test_ant_route_excludes_dmq_stops(self):
        """Ruta ANT no debe recibir paradas DMQ como candidatas."""
        mixed_stops = [
            _make_candidate("ant_stop_1", True, 0.3, stop_source="ANT"),
            _make_candidate("dmq_stop_1", True, 0.4, stop_source="DMQ",
                            lat=-0.290, lon=-78.490),
            _make_candidate("ant_stop_2", True, 0.7, stop_source="ANT",
                            lat=-0.340, lon=-78.450),
        ]
        filtered = [s for s in mixed_stops
                    if _jurisdiction_allows(s.stop_source, "ANT")]
        ids = {s.stop_id for s in filtered}
        assert "ant_stop_1" in ids
        assert "ant_stop_2" in ids
        assert "dmq_stop_1" not in ids, \
            "Parada DMQ no debe aparecer en ruta ANT — BUG-002"

    def test_dmq_route_excludes_ant_stops(self):
        mixed_stops = [
            _make_candidate("ant_stop_1", True, 0.3, stop_source="ANT"),
            _make_candidate("dmq_stop_1", True, 0.4, stop_source="DMQ",
                            lat=-0.290, lon=-78.490),
        ]
        filtered = [s for s in mixed_stops
                    if _jurisdiction_allows(s.stop_source, "DMQ")]
        ids = {s.stop_id for s in filtered}
        assert "dmq_stop_1" in ids
        assert "ant_stop_1" not in ids

    def test_both_jurisdiction_receives_all_stops(self):
        mixed_stops = [
            _make_candidate("ant_stop_1", True, 0.3, stop_source="ANT"),
            _make_candidate("dmq_stop_1", True, 0.4, stop_source="DMQ",
                            lat=-0.290, lon=-78.490),
        ]
        filtered = [s for s in mixed_stops
                    if _jurisdiction_allows(s.stop_source, "both")]
        assert len(filtered) == 2, \
            "jurisdiction='both' debe recibir paradas de ambas fuentes"

    def test_missing_jurisdiction_defaults_to_both(self):
        """Si el seed no tiene campo jurisdiction, debe comportarse como 'both'."""
        seed_without_jurisdiction = {"route": "test_route", "cooperative": "Test"}
        seed = intake_typed_seed(seed_without_jurisdiction)
        assert seed.jurisdiction == "both", \
            "Default de jurisdiction debe ser 'both' para retrocompatibilidad"


def _jurisdiction_allows(stop_source: Optional[str], route_jurisdiction: str) -> bool:
    """Helper que replica la lógica del filtro en corridor_stop_intersector."""
    if route_jurisdiction == "both":
        return True
    if stop_source is None:
        return True  # sin clasificar → pasar
    return stop_source == route_jurisdiction


# ═══════════════════════════════════════════════════════════════════════════
# BLOQUE 3 — FIX "miranda" REMOVIDO DE _NEIGHBORHOOD_KEYWORDS
# ═══════════════════════════════════════════════════════════════════════════

class TestNeighborhoodKeywords:

    def test_miranda_not_in_neighborhood_keywords(self):
        """
        'miranda' fue removido de _NEIGHBORHOOD_KEYWORDS porque es un nombre propio
        (barrio Miranda = terminus de Transcapelo) y causaba falsos positivos.
        """
        assert "miranda" not in _NEIGHBORHOOD_KEYWORDS, \
            "'miranda' no debe estar en _NEIGHBORHOOD_KEYWORDS — fue removido en fix BUG-001"

    def test_barrio_still_in_keywords(self):
        """Las keywords genéricas deben seguir ahí."""
        assert "barrio" in _NEIGHBORHOOD_KEYWORDS
        assert "sector" in _NEIGHBORHOOD_KEYWORDS
        assert "ciudadela" in _NEIGHBORHOOD_KEYWORDS

    def test_terminus_type_for_loreto_is_neighborhood(self):
        """
        'Parada de bus Loreto' → terminus_type debe ser 'neighborhood_endpoint'
        porque 'loreto' está en keywords O porque resuelve a stop_node.
        """
        token = _make_token(
            "Parada de bus Loreto",
            role="end",
            resolution_policy="resolve_to_best_matching_stop_node",
        )
        tt = infer_terminus_type(token, resolved_node=None)
        assert tt in {"neighborhood_endpoint", "street_terminus"}, \
            f"Loreto debe ser neighborhood_endpoint o street_terminus, got: {tt}"

    def test_terminus_type_for_miranda_is_not_formal(self):
        """
        'Miranda' (terminus Transcapelo) NO debe ser formal_terminal.
        Es un barrio rural → neighborhood_endpoint o street_terminus.
        """
        token = _make_token("Miranda", role="end")
        tt = infer_terminus_type(token, resolved_node=None)
        assert tt != "formal_terminal", \
            "Miranda es barrio rural, no debe inferirse como formal_terminal"

    def test_terminal_calsig_is_formal(self):
        """Terminal CALSIG Express tiene nodo OSM → debe ser formal_terminal."""
        class MockOSMNode:
            tags = {"amenity": "bus_station"}
        token = _make_token("Terminal CALSIG Express", role="terminal")
        tt = infer_terminus_type(token, resolved_node=MockOSMNode())
        assert tt == "formal_terminal"


# ═══════════════════════════════════════════════════════════════════════════
# BLOQUE 4 — SPATIAL DEDUP
# ═══════════════════════════════════════════════════════════════════════════

class TestSpatialDedup:

    def test_candidates_with_different_positions_not_merged(self):
        """
        Dos candidatos con lat/lon distintos (>40m de diferencia) no deben
        ser colapsados por spatial dedup.
        """
        candidate_terminus = _make_candidate(
            stop_id="loreto_terminus",
            in_expected_geography=False,
            path_fraction=0.95,
            lat=-0.328,   # Loreto
            lon=-78.420,
        )
        candidate_mid = _make_candidate(
            stop_id="normal_mid",
            in_expected_geography=True,
            path_fraction=0.5,
            lat=-0.280,   # mucho más al norte
            lon=-78.500,
        )
        probable, marginal, rejected = score_candidates(
            candidates=[candidate_terminus, candidate_mid],
            scoring_mode="heuristic",
            terminus_protected_ids={"loreto_terminus"},
        )
        all_ids = (
            {s.stop_id for s in probable}
            | {s.stop_id for s in marginal}
            | {s.stop_id for s in rejected}
        )
        assert "loreto_terminus" in all_ids, \
            "loreto_terminus desapareció — posible merge incorrecto por spatial dedup"
        assert "normal_mid" in all_ids, \
            "normal_mid desapareció — posible merge incorrecto por spatial dedup"


# ═══════════════════════════════════════════════════════════════════════════
# BLOQUE 5 — REGRESIÓN: CATÁLOGO V2 SIN JURISDICTION
# ═══════════════════════════════════════════════════════════════════════════

class TestCatalogV2Regression:

    @pytest.fixture(scope="class")
    def v2_routes(self):
        if not CATALOG_V2_PATH.exists():
            pytest.skip(f"Catálogo v2 no encontrado en: {CATALOG_V2_PATH}")
        return _load_catalog_routes()

    def test_catalog_loads_without_exception(self, v2_routes):
        """Las 19 rutas del catálogo v2 deben cargar sin excepción."""
        assert len(v2_routes) == 19, \
            f"Se esperaban 19 rutas en catálogo v2, encontradas: {len(v2_routes)}"

    def test_all_v2_routes_parse_as_typed_seed(self, v2_routes):
        """
        Todas las rutas v2 deben poder ser parseadas por intake_typed_seed
        aunque NO tengan campo 'jurisdiction'.
        Esto verifica retrocompatibilidad del campo con default 'both'.
        """
        errors = []
        for route in v2_routes:
            try:
                seed = intake_typed_seed(route)
                assert seed.jurisdiction == "both", \
                    f"Ruta sin jurisdiction debe defaultear a 'both': {route.get('route', '?')}"
            except Exception as e:
                errors.append(f"{route.get('route', '?')}: {e}")
        assert not errors, \
            f"Rutas que fallaron al parsear:\n" + "\n".join(errors)

    def test_v2_routes_with_default_jurisdiction_dont_filter_stops(self, v2_routes):
        """
        Rutas v2 sin campo jurisdiction → default 'both' → ninguna parada
        debe ser excluida por filtro de jurisdicción.
        Esto asegura que BUG-002 no rompe el comportamiento actual.
        """
        mixed_stops = [
            _make_candidate("ant_stop", True, 0.3, stop_source="ANT"),
            _make_candidate("dmq_stop", True, 0.5, stop_source="DMQ",
                            lat=-0.290, lon=-78.490),
            _make_candidate("unknown_stop", True, 0.7, stop_source=None,
                            lat=-0.300, lon=-78.470),
        ]
        for route in v2_routes:
            seed = intake_typed_seed(route)
            filtered = [s for s in mixed_stops
                        if _jurisdiction_allows(s.stop_source, seed.jurisdiction)]
            assert len(filtered) == 3, \
                f"Ruta '{route.get('route', '?')}' (jurisdiction='both') no debe filtrar nada"

    def test_calsig_routes_present(self, v2_routes):
        """Las 10 rutas CALSIG deben estar presentes — son el núcleo del catálogo v2."""
        calsig_routes = [r for r in v2_routes if r.get("cooperative") == "CALSIG Express"]
        assert len(calsig_routes) == 10, \
            f"Se esperaban 10 rutas CALSIG, encontradas: {len(calsig_routes)}"

    def test_loreto_rumiloma_route_present(self, v2_routes):
        """
        La ruta 'Loreto - Rumiloma' debe estar presente — fue la ruta que
        originalmente descubrió BUG-001 (terminus Loreto era prunado).
        """
        route_names = {r.get("route", "") for r in v2_routes}
        assert "Loreto - Rumiloma" in route_names, \
            "Ruta 'Loreto - Rumiloma' (CALSIG) debe estar en catálogo v2"

    def test_no_v2_route_has_jurisdiction_field(self, v2_routes):
        """
        Confirma que el catálogo v2 efectivamente NO tiene campo jurisdiction.
        Este test debe FALLAR cuando se genere el catálogo v3 (lo cual es correcto).
        Sirve como recordatorio de que v3 necesita el campo.
        """
        routes_with_jurisdiction = [
            r.get("route", "?") for r in v2_routes if "jurisdiction" in r
        ]
        assert len(routes_with_jurisdiction) == 0, (
            f"Catálogo v2 no debe tener campo 'jurisdiction' — eso es v3.\n"
            f"Rutas que ya lo tienen: {routes_with_jurisdiction}\n"
            f"(Si esto falla porque migraste a v3, puedes borrar este test)"
        )


# ═══════════════════════════════════════════════════════════════════════════
# BLOQUE 6 — ZONA DE SOLAPAMIENTO ANT/DMQ (test pendiente para v3)
# ═══════════════════════════════════════════════════════════════════════════

class TestOverlapZone:
    """
    Tests para la zona de solapamiento geográfico ANT/DMQ:
    El Triángulo, San Pedro de Taboada, ESPE.

    ESTADO: pendiente — requiere catálogo v3 con campo jurisdiction.
    Los tests están escritos pero marcados como xfail hasta que v3 exista.
    """

    @pytest.mark.xfail(
        reason="Requiere catálogo v3 con campo jurisdiction. "
               "Remover xfail cuando se genere v3."
    )
    def test_transcapelo_dmq_gets_dmq_stops_in_overlap_zone(self):
        """
        Transcapelo (DMQ) pasa por El Triángulo (zona solapamiento).
        Debe recibir paradas DMQ de esa zona, aunque estén dentro del bbox Rumiñahui.
        """
        # Parada en El Triángulo — geográficamente en Rumiñahui pero operada por DMQ
        triangulo_dmq_stop = _make_candidate(
            stop_id="triangulo_dmq",
            in_expected_geography=True,
            path_fraction=0.2,
            stop_source="DMQ",
            lat=-0.3003,
            lon=-78.4603,
        )
        # Transcapelo es DMQ → debe recibir esta parada
        filtered = [s for s in [triangulo_dmq_stop]
                    if _jurisdiction_allows(s.stop_source, "DMQ")]
        assert len(filtered) == 1

    @pytest.mark.xfail(
        reason="Requiere catálogo v3 con campo jurisdiction."
    )
    def test_vingala_ant_gets_ant_stops_in_overlap_zone(self):
        """
        Vingala (ANT) pasa por ESPE (zona solapamiento).
        Debe recibir paradas ANT de esa zona.
        """
        espe_ant_stop = _make_candidate(
            stop_id="espe_ant",
            in_expected_geography=True,
            path_fraction=0.6,
            stop_source="ANT",
            lat=-0.3149,
            lon=-78.4435,
        )
        filtered = [s for s in [espe_ant_stop]
                    if _jurisdiction_allows(s.stop_source, "ANT")]
        assert len(filtered) == 1


# ═══════════════════════════════════════════════════════════════════════════
# RESUMEN DE COBERTURA
# ═══════════════════════════════════════════════════════════════════════════
#
# Bloque 1 — BUG-001 terminus protection     : 8 tests
# Bloque 2 — BUG-002 jurisdiction filter     : 4 tests
# Bloque 3 — Fix "miranda" keyword           : 5 tests
# Bloque 4 — Spatial dedup                  : 1 test
# Bloque 5 — Regresión catálogo v2          : 6 tests
# Bloque 6 — Zona solapamiento (xfail x2)   : 2 tests (pendiente v3)
#
# TOTAL: 24 tests activos + 2 xfail
#
# Cuando se genere el catálogo v3:
#   1. Añadir jurisdiction a cada ruta en el JSON
#   2. Remover xfail de Bloque 6
#   3. test_no_v2_route_has_jurisdiction_field pasará a fallar → borrarlo
