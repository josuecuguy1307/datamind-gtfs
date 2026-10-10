"""rerank combines semantic, alias, evidence and type signals with fixed weights."""
import pytest

from src.pipeline.search import rerank as r


def test_weights_sum_to_one() -> None:
    assert r.W_SEMANTIC + r.W_ALIAS + r.W_EVIDENCE + r.W_TYPE == pytest.approx(1.0)


def test_alias_score_exact_partial_and_none() -> None:
    assert r.alias_score("Quitumbe", ["quitumbe"]) == 1.0
    assert r.alias_score("quitumbe", ["terminal quitumbe"]) == 0.7
    assert r.alias_score("ejido", ["carcelen"]) == 0.0
    assert r.alias_score("ejido", []) == 0.0


def test_evidence_score_is_clipped_at_one() -> None:
    assert r.evidence_score([{"weight": 0.6}, {"weight": 0.7}]) == 1.0
    assert r.evidence_score([{"weight": 0.3}]) == pytest.approx(0.3)
    assert r.evidence_score([]) == 0.0


def test_type_score_matches_query_intent() -> None:
    assert r.type_score("parada el ejido", "stop") == 1.0
    assert r.type_score("hospital metropolitano", "poi") == 1.0
    assert r.type_score("hospital metropolitano", "stop") == 0.0


def test_rerank_orders_by_final_score_and_explains_it() -> None:
    candidates = [
        {"id": "a", "score": 0.80, "payload": {"aliases": ["mall el jardin"], "kind": "poi"}},
        {"id": "b", "score": 0.70, "payload": {"aliases": ["parada el jardin"], "kind": "stop", "evidence": [{"weight": 1.0}]}},
    ]
    out = r.rerank("parada el jardin", candidates)
    assert [c["id"] for c in out] == ["b", "a"]  # alias + evidence + type outweigh 0.10 of semantic score
    top = out[0]
    assert top["final_score"] == pytest.approx(0.60 * 0.70 + 0.20 * 1.0 + 0.15 * 1.0 + 0.05 * 1.0)
    assert set(top["score_components"]) == {"semantic", "alias", "evidence", "type"}
    assert candidates[0].get("final_score") is None  # input is not mutated
