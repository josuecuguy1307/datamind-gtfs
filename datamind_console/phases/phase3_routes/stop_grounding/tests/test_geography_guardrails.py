import sys
from pathlib import Path

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "datamind_console").exists())
sys.path.insert(0, str(ROOT))

from datamind_console.phases.phase3_routes.stop_grounding.geography_guardrails import (
    derive_expected_geographic_envelope,
    derive_hint_geography_context,
    grounding_text_alignment,
    infer_locality_keys,
)


def test_infer_locality_keys_prefers_specific_redondel_key():
    keys = infer_locality_keys(["Redondel del Choclo"])
    assert keys
    assert keys[0] == "redondel del choclo"


def test_hint_geography_context_resolves_los_tubos_proxy():
    envelope = derive_expected_geographic_envelope(
        route_name="Los Tubos - Sangolqui - Quito",
        operator_name="Condorvall",
        corridor_description="Los Tubos -> Sangolqui -> El Trebol / La Marin",
        anchor_a_hint="Los Tubos",
        anchor_b_hint="El Trebol / La Marin",
        intermediate_hints=["Sangolqui", "San Rafael"],
        locality_hints=["los tubos", "sangolqui", "la marin"],
        sequence_seed_fragments=["Los Tubos", "Sangolqui", "San Rafael", "El Trebol / La Marin"],
    )
    ctx = derive_hint_geography_context("Los Tubos", envelope=envelope)
    assert ctx["proxy_key"] == "los tubos"
    assert ctx["proxy_lon"] is not None
    assert ctx["proxy_lat"] is not None


def test_grounding_text_alignment_flags_hard_mismatch():
    meta = grounding_text_alignment(
        hint_text="Los Tubos",
        stop_name="Los Guabos",
        locality="",
        ref="",
    )
    assert meta["text_alignment_score"] == 0.0
    assert meta["hard_token_mismatch"] is True


def test_grounding_text_alignment_rewards_exact_locality_family():
    meta = grounding_text_alignment(
        hint_text="Parada de los Valles / La Marin",
        stop_name="Playon de la Marin",
        locality="",
        ref="",
    )
    assert meta["text_alignment_score"] >= 0.8
    assert meta["hard_token_mismatch"] is False
