from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from datamind_console.common.extractor_input_contracts import derive_phase3_route_hint_contract


class ExtractorInputContractTests(unittest.TestCase):
    # --- existing tests ---

    def test_phase3_route_hint_keeps_plain_name_only(self) -> None:
        got = derive_phase3_route_hint_contract("Ecovia")
        self.assertEqual(got.get("refs"), [])
        self.assertEqual(got.get("name"), "Ecovia")
        self.assertIsNone(got.get("service_route_ref"))

    def test_phase3_route_hint_extracts_ref_only(self) -> None:
        got = derive_phase3_route_hint_contract("E1")
        self.assertEqual(got.get("refs"), ["E1"])
        self.assertIsNone(got.get("name"))
        self.assertEqual(got.get("service_route_ref"), "E1")

    def test_phase3_route_hint_splits_ref_from_name_words(self) -> None:
        got = derive_phase3_route_hint_contract("E1 Ecovia")
        self.assertEqual(got.get("refs"), ["E1"])
        self.assertEqual(got.get("name"), "Ecovia")

    def test_phase3_route_hint_uses_catalog_aliases(self) -> None:
        got = derive_phase3_route_hint_contract(
            "Chillos Line 1",
            ref_catalog=[{"key": "CH1", "label": "Chillos Line 1"}],
        )
        self.assertEqual(got.get("refs"), ["CH1"])
        self.assertIsNone(got.get("name"))

    def test_phase3_route_hint_handles_mixed_segments(self) -> None:
        got = derive_phase3_route_hint_contract("E1, Ecovia trunk")
        self.assertEqual(got.get("refs"), ["E1"])
        self.assertEqual(got.get("name"), "Ecovia trunk")

    # --- transport mode filtering ---

    def test_transport_mode_word_filtered_from_name(self) -> None:
        got = derive_phase3_route_hint_contract("bus Ecovia")
        self.assertEqual(got.get("refs"), [])
        self.assertEqual(got.get("name"), "Ecovia")

    def test_multiple_transport_mode_words_filtered(self) -> None:
        got = derive_phase3_route_hint_contract("metro bus E1")
        self.assertEqual(got.get("refs"), ["E1"])
        self.assertIsNone(got.get("name"))

    def test_transport_mode_alone_gives_unstructured(self) -> None:
        got = derive_phase3_route_hint_contract("bus")
        self.assertEqual(got.get("refs"), [])
        self.assertIsNone(got.get("name"))
        self.assertEqual(got.get("hint_strength"), "unstructured")

    # --- corridor dash patterns ---

    def test_corridor_dash_becomes_name_not_ref(self) -> None:
        got = derive_phase3_route_hint_contract("Conocoto - San Rafael")
        self.assertEqual(got.get("refs"), [])
        self.assertIn("Conocoto", got.get("name") or "")
        self.assertIn("San Rafael", got.get("name") or "")

    def test_corridor_dash_with_ref_left(self) -> None:
        got = derive_phase3_route_hint_contract("E1 - San Rafael")
        self.assertEqual(got.get("refs"), ["E1"])
        self.assertEqual(got.get("name"), "San Rafael")

    def test_corridor_dash_with_ref_right(self) -> None:
        got = derive_phase3_route_hint_contract("Ecovia - E1")
        self.assertEqual(got.get("refs"), ["E1"])
        self.assertEqual(got.get("name"), "Ecovia")

    # --- cooperative / operator signal ---

    def test_cooperative_signal_extracted(self) -> None:
        got = derive_phase3_route_hint_contract("Cooperativa Sample Region, E1")
        self.assertIsNotNone(got.get("operator_signal"))
        self.assertIn("Sample Region", got.get("operator_signal") or "")
        self.assertEqual(got.get("refs"), ["E1"])

    def test_cooperative_signal_alone(self) -> None:
        got = derive_phase3_route_hint_contract("Cooperativa Sample Region")
        self.assertIsNotNone(got.get("operator_signal"))
        self.assertEqual(got.get("hint_strength"), "moderate")

    # --- hint strength classification ---

    def test_hint_strength_strong_with_refs(self) -> None:
        got = derive_phase3_route_hint_contract("E1")
        self.assertEqual(got.get("hint_strength"), "strong")

    def test_hint_strength_strong_with_refs_and_name(self) -> None:
        got = derive_phase3_route_hint_contract("E1, Ecovia")
        self.assertEqual(got.get("hint_strength"), "strong")

    def test_hint_strength_moderate_multiword_name(self) -> None:
        got = derive_phase3_route_hint_contract("Ecovia trunk")
        self.assertEqual(got.get("hint_strength"), "moderate")

    def test_hint_strength_weak_single_name(self) -> None:
        got = derive_phase3_route_hint_contract("Ecovia")
        self.assertEqual(got.get("hint_strength"), "weak")

    def test_hint_strength_empty_for_blank(self) -> None:
        got = derive_phase3_route_hint_contract("")
        self.assertEqual(got.get("hint_strength"), "empty")

    def test_hint_strength_empty_for_none(self) -> None:
        got = derive_phase3_route_hint_contract(None)
        self.assertEqual(got.get("hint_strength"), "empty")

    # --- mixed ref/name edge cases ---

    def test_multiple_refs_in_segments(self) -> None:
        got = derive_phase3_route_hint_contract("E1, CH1")
        self.assertEqual(got.get("refs"), ["E1", "CH1"])
        self.assertEqual(got.get("service_route_ref"), "E1")
        self.assertIsNone(got.get("name"))

    def test_ref_with_trailing_noise(self) -> None:
        got = derive_phase3_route_hint_contract("E1 bus route")
        self.assertEqual(got.get("refs"), ["E1"])
        # "bus" and "route" are transport/generic words, should be filtered
        name = got.get("name")
        self.assertTrue(name is None or "bus" not in name.lower())

    def test_generic_route_hint_name_cleared_when_refs_present(self) -> None:
        got = derive_phase3_route_hint_contract("E1, route")
        self.assertEqual(got.get("refs"), ["E1"])
        # "route" is a generic hint name and should be cleared when refs exist
        self.assertIsNone(got.get("name"))


class OutputContractTests(unittest.TestCase):
    """Verify the output dict always has the expected keys."""

    def test_all_expected_keys_present(self) -> None:
        got = derive_phase3_route_hint_contract("E1 Ecovia")
        expected_keys = {"route_hint", "refs", "name", "service_route_ref", "operator_signal", "hint_strength"}
        self.assertTrue(expected_keys.issubset(set(got.keys())), f"Missing keys: {expected_keys - set(got.keys())}")

    def test_empty_input_all_keys_present(self) -> None:
        got = derive_phase3_route_hint_contract("")
        expected_keys = {"route_hint", "refs", "name", "service_route_ref", "operator_signal", "hint_strength"}
        self.assertTrue(expected_keys.issubset(set(got.keys())))


if __name__ == "__main__":
    unittest.main()
