"""Tests for side-of-road preprocessing module."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from src.constructor_v2.preprocessing.side_of_road import (
    SideOfRoadPreprocessor,
    PreprocessorConfig,
    _levenshtein_ratio,
)
from src.constructor_v2.schemas.route_input import NormalizedStop


def _make_stop(
    stop_id: str,
    name: str,
    lat: float,
    lon: float,
    *,
    is_fixed_start: bool = False,
    is_fixed_end: bool = False,
    is_known_anchor: bool = False,
    weak: bool = False,
    score: float = 0.8,
) -> NormalizedStop:
    return NormalizedStop(
        order_hint=0,
        stop_id=stop_id,
        stop_name=name,
        lat=lat,
        lon=lon,
        source_stop_ids=(stop_id,),
        source_indices=(1,),
        source_names=(name,),
        representative_score=score,
        weak_candidate=weak,
        optional_penalty=None,
        is_fixed_start=is_fixed_start,
        is_fixed_end=is_fixed_end,
        is_known_anchor=is_known_anchor,
    )


def _mock_client(locate_data=None, supports_locate=True):
    client = MagicMock()
    client.supports.return_value = supports_locate
    if locate_data is not None:
        client.locate.return_value = locate_data
    else:
        client.locate.return_value = []
    return client


class TestDirectionFilter:
    def test_opposite_direction_stop_flagged(self):
        """Stop on road going opposite to corridor should be flagged."""
        stops = [
            _make_stop("start", "Start", -0.30, -78.50, is_fixed_start=True, is_known_anchor=True),
            _make_stop("mid", "Middle", -0.31, -78.51),
            _make_stop("bad", "Wrong Way", -0.32, -78.52),
            _make_stop("end", "End", -0.35, -78.55, is_fixed_end=True, is_known_anchor=True),
        ]
        # Corridor goes roughly south (bearing ~200). bad stop has edge heading ~20 (north) = opposite
        locate_data = [
            {"edges": [{"heading": 200, "distance": 5}]},  # start: aligned
            {"edges": [{"heading": 210, "distance": 5}]},  # mid: aligned
            {"edges": [{"heading": 20, "distance": 5}]},   # bad: opposite
            {"edges": [{"heading": 200, "distance": 5}]},  # end: aligned
        ]
        client = _mock_client(locate_data)
        preprocessor = SideOfRoadPreprocessor(client)
        result = preprocessor.preprocess(stops)
        assert "bad" in result.flagged_reasons
        assert "opposite_direction" in result.flagged_reasons["bad"]
        assert result.stats["opposite_direction"] >= 1

    def test_terminus_never_flagged(self):
        """Terminus stops should never be flagged even if edge opposes corridor."""
        stops = [
            _make_stop("start", "Start", -0.30, -78.50, is_fixed_start=True, is_known_anchor=True),
            _make_stop("end", "End", -0.35, -78.55, is_fixed_end=True, is_known_anchor=True),
        ]
        locate_data = [
            {"edges": [{"heading": 20, "distance": 5}]},   # opposite but terminus
            {"edges": [{"heading": 20, "distance": 5}]},   # opposite but terminus
        ]
        client = _mock_client(locate_data)
        preprocessor = SideOfRoadPreprocessor(client)
        result = preprocessor.preprocess(stops)
        assert "start" not in result.flagged_reasons
        assert "end" not in result.flagged_reasons

    def test_anchor_never_removed(self):
        """Anchor stops should never be flagged."""
        stops = [
            _make_stop("start", "Start", -0.30, -78.50, is_fixed_start=True, is_known_anchor=True),
            _make_stop("anchor", "Anchor", -0.32, -78.52, is_known_anchor=True),
            _make_stop("end", "End", -0.35, -78.55, is_fixed_end=True, is_known_anchor=True),
        ]
        locate_data = [
            {"edges": [{"heading": 200, "distance": 5}]},
            {"edges": [{"heading": 20, "distance": 5}]},   # opposite but anchor
            {"edges": [{"heading": 200, "distance": 5}]},
        ]
        client = _mock_client(locate_data)
        preprocessor = SideOfRoadPreprocessor(client)
        result = preprocessor.preprocess(stops)
        assert "anchor" not in result.flagged_reasons


class TestDuplicateCollapse:
    def test_duplicates_within_30m_flagged(self):
        """Stops within 30m with similar names should have one flagged as duplicate."""
        stops = [
            _make_stop("start", "Start", -0.30, -78.50, is_fixed_start=True, is_known_anchor=True),
            _make_stop("a1", "Parada Central", -0.310000, -78.510000),
            _make_stop("a2", "Parada Central", -0.310001, -78.510001),  # ~1m away, same name
            _make_stop("end", "End", -0.35, -78.55, is_fixed_end=True, is_known_anchor=True),
        ]
        client = _mock_client(supports_locate=False)
        preprocessor = SideOfRoadPreprocessor(client, config=PreprocessorConfig(enable_direction_filter=False))
        result = preprocessor.preprocess(stops)
        assert result.stats["duplicates"] >= 1
        # One of a1/a2 should be flagged
        flagged_dup = [sid for sid, reason in result.flagged_reasons.items() if "duplicate" in reason]
        assert len(flagged_dup) >= 1

    def test_terminus_not_collapsed(self):
        """Terminus stops should never be collapsed even if close and same name."""
        stops = [
            _make_stop("start", "Terminal", -0.30, -78.50, is_fixed_start=True, is_known_anchor=True),
            _make_stop("near_start", "Terminal", -0.300001, -78.500001),
            _make_stop("end", "End", -0.35, -78.55, is_fixed_end=True, is_known_anchor=True),
        ]
        client = _mock_client(supports_locate=False)
        preprocessor = SideOfRoadPreprocessor(client, config=PreprocessorConfig(enable_direction_filter=False))
        result = preprocessor.preprocess(stops)
        assert "start" not in result.flagged_reasons


class TestOutlierDetection:
    def test_outlier_flagged(self):
        """Stop far from all others should be flagged as outlier."""
        stops = [
            _make_stop("start", "Start", -0.30, -78.50, is_fixed_start=True, is_known_anchor=True),
            _make_stop("a", "Stop A", -0.301, -78.501),
            _make_stop("b", "Stop B", -0.302, -78.502),
            _make_stop("c", "Stop C", -0.303, -78.503),
            _make_stop("outlier", "Far Away", -0.50, -78.70),  # ~30km away
            _make_stop("end", "End", -0.305, -78.505, is_fixed_end=True, is_known_anchor=True),
        ]
        client = _mock_client(supports_locate=False)
        preprocessor = SideOfRoadPreprocessor(
            client,
            config=PreprocessorConfig(enable_direction_filter=False, enable_duplicate_filter=False),
        )
        result = preprocessor.preprocess(stops)
        assert "outlier" in result.flagged_reasons
        assert result.stats["outliers"] >= 1

    def test_terminus_not_flagged_as_outlier(self):
        """Terminus should not be flagged as outlier even if far."""
        stops = [
            _make_stop("start", "Start", -0.30, -78.50, is_fixed_start=True, is_known_anchor=True),
            _make_stop("a", "Stop A", -0.301, -78.501),
            _make_stop("end", "End", -0.50, -78.70, is_fixed_end=True, is_known_anchor=True),
        ]
        client = _mock_client(supports_locate=False)
        preprocessor = SideOfRoadPreprocessor(
            client,
            config=PreprocessorConfig(enable_direction_filter=False, enable_duplicate_filter=False),
        )
        result = preprocessor.preprocess(stops)
        assert "end" not in result.flagged_reasons


class TestLevenshtein:
    def test_identical_strings(self):
        assert _levenshtein_ratio("hello", "hello") == 1.0

    def test_similar_strings(self):
        ratio = _levenshtein_ratio("parada central", "parada centrol")
        assert ratio > 0.7

    def test_empty_strings(self):
        assert _levenshtein_ratio("", "") == 1.0
        assert _levenshtein_ratio("abc", "") == 0.0


class TestPreprocessorResult:
    def test_all_stops_returned(self):
        """All stops should be in cleaned_stops (we flag, not remove)."""
        stops = [
            _make_stop("start", "Start", -0.30, -78.50, is_fixed_start=True, is_known_anchor=True),
            _make_stop("mid", "Mid", -0.31, -78.51),
            _make_stop("end", "End", -0.35, -78.55, is_fixed_end=True, is_known_anchor=True),
        ]
        client = _mock_client(supports_locate=False)
        preprocessor = SideOfRoadPreprocessor(client)
        result = preprocessor.preprocess(stops)
        assert len(result.cleaned_stops) == 3
        assert result.removed_stop_ids == []

    def test_two_stops_no_crash(self):
        """Should handle minimal stop set."""
        stops = [
            _make_stop("start", "Start", -0.30, -78.50, is_fixed_start=True, is_known_anchor=True),
            _make_stop("end", "End", -0.35, -78.55, is_fixed_end=True, is_known_anchor=True),
        ]
        client = _mock_client(supports_locate=False)
        preprocessor = SideOfRoadPreprocessor(client)
        result = preprocessor.preprocess(stops)
        assert len(result.cleaned_stops) == 2
