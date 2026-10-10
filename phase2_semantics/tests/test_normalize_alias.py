"""normalize_alias is the single source of truth for alias search keys."""
import pytest

from src.pipeline.normalize.normalize_alias import normalize_alias


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Estación  Quitumbe", "estacion quitumbe"),
        ("  PARADA   El Ejido ", "parada el ejido"),
        ("Av. 10 de Agosto", "av 10 de agosto"),
        ("Ñuñoa", "nunoa"),
        ("C.C. Quicentro-Sur!", "c c quicentro sur"),
    ],
)
def test_lowercases_strips_accents_punctuation_and_spaces(raw: str, expected: str) -> None:
    assert normalize_alias(raw) == expected


@pytest.mark.parametrize("empty", [None, ""])
def test_empty_input_returns_none(empty) -> None:
    assert normalize_alias(empty) is None


def test_is_idempotent() -> None:
    once = normalize_alias("Terminal Río Coca (Norte)")
    assert normalize_alias(once) == once
