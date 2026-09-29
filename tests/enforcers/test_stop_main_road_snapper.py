"""Unit tests for the Stop-to-Main-Road Snapper."""
from __future__ import annotations

from hades.enforcers.stop_main_road_snapper import snap_stops_to_main_road


def _overpass_stub_factory(scenarios: dict[tuple[float, float], list[dict]]):
    def _fn(lat, lon, radius_m):
        key = (round(lat, 6), round(lon, 6))
        return list(scenarios.get(key, []))
    return _fn


def test_stop_on_service_with_primary_nearby_is_snapped():
    stop = (-0.20000, -78.50000)
    # Service road exactly at the stop; primary 20 m east.
    scenarios = {
        (round(stop[0], 6), round(stop[1], 6)): [
            {"way_id": 1, "highway": "service", "distance_m": 2.0,
             "geometry": [stop]},
            {"way_id": 2, "highway": "primary", "distance_m": 20.0,
             # primary runs N-S at lon = -78.49980 (~22 m east)
             "geometry": [(-0.19990, -78.49980), (-0.20010, -78.49980)]},
        ],
    }
    r = snap_stops_to_main_road(
        route_id="t1", stops=[stop],
        overpass_fn=_overpass_stub_factory(scenarios),
    )
    assert r.n_moved == 1
    assert r.snaps[0].reason == "snapped"
    assert r.snaps[0].best_highway == "primary"
    assert r.snaps[0].move_distance_m > 0


def test_stop_already_on_primary_is_not_moved():
    stop = (-0.20000, -78.50000)
    scenarios = {
        (round(stop[0], 6), round(stop[1], 6)): [
            {"way_id": 1, "highway": "primary", "distance_m": 3.0,
             "geometry": [(-0.19990, -78.50000), (-0.20010, -78.50000)]},
            {"way_id": 2, "highway": "secondary", "distance_m": 45.0,
             "geometry": [(-0.19955, -78.50000), (-0.19960, -78.50000)]},
        ],
    }
    r = snap_stops_to_main_road(
        route_id="t2", stops=[stop],
        overpass_fn=_overpass_stub_factory(scenarios),
    )
    assert r.n_moved == 0
    assert r.snaps[0].reason == "kept_current_is_main"


def test_stop_on_service_with_no_upgrade_is_not_moved():
    stop = (-0.20000, -78.50000)
    scenarios = {
        (round(stop[0], 6), round(stop[1], 6)): [
            {"way_id": 1, "highway": "service", "distance_m": 2.0,
             "geometry": [stop]},
            {"way_id": 2, "highway": "residential", "distance_m": 15.0,
             "geometry": [(-0.19985, -78.49990), (-0.19990, -78.49995)]},
        ],
    }
    r = snap_stops_to_main_road(
        route_id="t3", stops=[stop],
        overpass_fn=_overpass_stub_factory(scenarios),
    )
    assert r.n_moved == 0
    assert r.snaps[0].reason == "no_upgrade"


def test_stop_on_service_with_primary_too_far_is_not_moved():
    stop = (-0.20000, -78.50000)
    scenarios = {
        (round(stop[0], 6), round(stop[1], 6)): [
            {"way_id": 1, "highway": "service", "distance_m": 2.0,
             "geometry": [stop]},
            # primary 80m away — beyond max_distance_m=50
            {"way_id": 2, "highway": "primary", "distance_m": 80.0,
             "geometry": [(-0.19928, -78.49990), (-0.19930, -78.49995)]},
        ],
    }
    r = snap_stops_to_main_road(
        route_id="t4", stops=[stop],
        overpass_fn=_overpass_stub_factory(scenarios),
    )
    assert r.n_moved == 0
    assert r.snaps[0].reason == "target_too_far"


def test_empty_overpass_response_leaves_stop_untouched():
    stop = (-0.20000, -78.50000)
    r = snap_stops_to_main_road(
        route_id="t5", stops=[stop],
        overpass_fn=lambda lat, lon, r: [],
    )
    assert r.n_moved == 0
    assert r.snaps[0].reason == "no_roads_nearby"


def test_motorway_only_neighbor_is_rejected_forbidden():
    stop = (-0.20000, -78.50000)
    scenarios = {
        (round(stop[0], 6), round(stop[1], 6)): [
            {"way_id": 1, "highway": "service", "distance_m": 2.0,
             "geometry": [stop]},
            # Only other nearby option is a motorway — forbidden target.
            {"way_id": 2, "highway": "motorway", "distance_m": 15.0,
             "geometry": [(-0.19990, -78.49985), (-0.20010, -78.49985)]},
        ],
    }
    r = snap_stops_to_main_road(
        route_id="t6", stops=[stop],
        overpass_fn=_overpass_stub_factory(scenarios),
    )
    # The service is also forbidden? No — `service` is allowed as "current"
    # but the motorway is filtered out of eligibility; the only remaining
    # eligible road is the service itself which gives no upgrade.
    assert r.n_moved == 0
    assert r.snaps[0].reason == "no_upgrade"


def test_motorway_plus_primary_picks_primary_not_motorway():
    stop = (-0.20000, -78.50000)
    scenarios = {
        (round(stop[0], 6), round(stop[1], 6)): [
            {"way_id": 1, "highway": "service", "distance_m": 2.0,
             "geometry": [stop]},
            {"way_id": 2, "highway": "motorway", "distance_m": 12.0,
             "geometry": [(-0.19990, -78.49988), (-0.20010, -78.49988)]},
            {"way_id": 3, "highway": "primary", "distance_m": 25.0,
             "geometry": [(-0.19975, -78.49975), (-0.20025, -78.49975)]},
        ],
    }
    r = snap_stops_to_main_road(
        route_id="t7", stops=[stop],
        overpass_fn=_overpass_stub_factory(scenarios),
    )
    assert r.n_moved == 1
    assert r.snaps[0].best_highway == "primary"
    assert r.snaps[0].reason == "snapped"


# ---------------------------------------------------------------------------
# v2 gate tests — continuity + named-road.
# ---------------------------------------------------------------------------

def _corridor_scenario(stop0, stop1, stop2, primary_name="Av. Amazonas",
                      candidate_on_prev=True, candidate_on_next=True,
                      neighbour_has_primary_with_name=True):
    """Build an Overpass stub for a 3-stop route where:
    - middle stop (stop1) sits on a service road
    - a primary road with way_id=99 is within 50m of stop1
    - depending on flags, that same primary is also within 50m of
      prev (stop0) and next (stop2)
    - neighbour stops may also have a different primary (different name)
    """
    target_geom = [(-0.19990, -78.49985), (-0.20010, -78.49985)]
    target = {"way_id": 99, "highway": "primary", "name": primary_name,
              "distance_m": 25.0, "geometry": target_geom}
    other_primary = {"way_id": 50, "highway": "primary",
                     "name": "Av. 10 de Agosto", "distance_m": 15.0,
                     "geometry": [(-0.19995, -78.50005), (-0.20005, -78.50005)]}
    service = lambda s: {"way_id": 1, "highway": "service", "distance_m": 2.0,
                         "geometry": [s]}
    scenarios = {
        (round(stop0[0], 6), round(stop0[1], 6)): (
            ([target] if candidate_on_prev else [])
            + ([other_primary] if neighbour_has_primary_with_name else [])
            + [service(stop0)]
        ),
        (round(stop1[0], 6), round(stop1[1], 6)): [service(stop1), target],
        (round(stop2[0], 6), round(stop2[1], 6)): (
            ([target] if candidate_on_next else [])
            + ([other_primary] if neighbour_has_primary_with_name else [])
            + [service(stop2)]
        ),
    }
    return scenarios


def test_v2_continuity_gate_accepts_when_prev_and_next_reach_target():
    stops = [(-0.20000, -78.50020), (-0.20000, -78.50000), (-0.20000, -78.49980)]
    scn = _corridor_scenario(*stops, candidate_on_prev=True, candidate_on_next=True,
                             neighbour_has_primary_with_name=False)
    r = snap_stops_to_main_road(
        route_id="tcg-pass", stops=stops,
        overpass_fn=_overpass_stub_factory(scn),
        require_continuity=True,
    )
    # Middle stop on service with target Av. Amazonas (name=primary); both
    # neighbours have a road named "Av. Amazonas" near them → name continuity
    # passes.
    assert any(s.moved for s in r.snaps), f"expected ≥1 snap, got: {[s.reason for s in r.snaps]}"
    assert r.snaps[1].reason == "snapped"


def test_v2_continuity_gate_rejects_when_next_cannot_reach_target():
    stops = [(-0.20000, -78.50020), (-0.20000, -78.50000), (-0.20000, -78.49980)]
    scn = _corridor_scenario(*stops, candidate_on_prev=True, candidate_on_next=False,
                             neighbour_has_primary_with_name=False)
    r = snap_stops_to_main_road(
        route_id="tcg-fail", stops=stops,
        overpass_fn=_overpass_stub_factory(scn),
        require_continuity=True,
    )
    assert r.snaps[1].moved is False
    # Name only present at prev (next has no copy of the avenue) → one-side-only
    assert r.snaps[1].reason == "gate_continuity_failed_one_side_only"


# ---------------------------------------------------------------------------
# v2b — way-NAME continuity tests (Option B).
# ---------------------------------------------------------------------------

def test_v2b_named_continuity_accepts_when_avenue_name_matches_both_sides():
    """Both neighbours have a tertiary+ road tagged "Av. Amazonas" (with
    different way_ids — OSM fragmentation). Name continuity should pass."""
    stops = [(-0.20000, -78.50020), (-0.20000, -78.50000), (-0.20000, -78.49980)]
    target = {"way_id": 99, "highway": "primary", "name": "Av. Amazonas",
              "distance_m": 25.0,
              "geometry": [(-0.19990, -78.49985), (-0.20010, -78.49985)]}
    # Each neighbour has a DIFFERENT way_id but the SAME name as the target
    prev_avenue = {"way_id": 88, "highway": "primary", "name": "Av. Amazonas",
                   "distance_m": 22.0,
                   "geometry": [(-0.19998, -78.50005), (-0.20002, -78.50005)]}
    next_avenue = {"way_id": 77, "highway": "primary", "name": "Av. Amazonas",
                   "distance_m": 24.0,
                   "geometry": [(-0.19998, -78.49975), (-0.20002, -78.49975)]}
    service = lambda s: {"way_id": 1, "highway": "service", "distance_m": 2.0,
                         "geometry": [s]}
    scn = {
        (round(stops[0][0], 6), round(stops[0][1], 6)): [prev_avenue, service(stops[0])],
        (round(stops[1][0], 6), round(stops[1][1], 6)): [service(stops[1]), target],
        (round(stops[2][0], 6), round(stops[2][1], 6)): [next_avenue, service(stops[2])],
    }
    r = snap_stops_to_main_road(
        route_id="t-name-pass", stops=stops,
        overpass_fn=_overpass_stub_factory(scn),
        require_continuity=True,
    )
    assert r.snaps[1].moved is True
    assert r.snaps[1].reason == "snapped"


def test_v2b_named_continuity_rejects_when_name_only_matches_prev():
    stops = [(-0.20000, -78.50020), (-0.20000, -78.50000), (-0.20000, -78.49980)]
    target = {"way_id": 99, "highway": "primary", "name": "Av. Amazonas",
              "distance_m": 25.0,
              "geometry": [(-0.19990, -78.49985), (-0.20010, -78.49985)]}
    # prev has Av. Amazonas; next has Av. Different
    prev_avenue = {"way_id": 88, "highway": "primary", "name": "Av. Amazonas",
                   "distance_m": 22.0,
                   "geometry": [(-0.19998, -78.50005), (-0.20002, -78.50005)]}
    next_avenue = {"way_id": 77, "highway": "primary", "name": "Av. Different",
                   "distance_m": 24.0,
                   "geometry": [(-0.19998, -78.49975), (-0.20002, -78.49975)]}
    service = lambda s: {"way_id": 1, "highway": "service", "distance_m": 2.0,
                         "geometry": [s]}
    scn = {
        (round(stops[0][0], 6), round(stops[0][1], 6)): [prev_avenue, service(stops[0])],
        (round(stops[1][0], 6), round(stops[1][1], 6)): [service(stops[1]), target],
        (round(stops[2][0], 6), round(stops[2][1], 6)): [next_avenue, service(stops[2])],
    }
    r = snap_stops_to_main_road(
        route_id="t-name-half", stops=stops,
        overpass_fn=_overpass_stub_factory(scn),
        require_continuity=True,
    )
    assert r.snaps[1].moved is False
    assert r.snaps[1].reason == "gate_continuity_failed_one_side_only"


def test_v2b_named_continuity_rejects_when_target_has_no_name_tag():
    stops = [(-0.20000, -78.50020), (-0.20000, -78.50000), (-0.20000, -78.49980)]
    # Target is primary but has NO `name` tag (rare but happens)
    target = {"way_id": 99, "highway": "primary", "name": None,
              "distance_m": 25.0,
              "geometry": [(-0.19990, -78.49985), (-0.20010, -78.49985)]}
    service = lambda s: {"way_id": 1, "highway": "service", "distance_m": 2.0,
                         "geometry": [s]}
    scn = {
        (round(stops[0][0], 6), round(stops[0][1], 6)): [service(stops[0])],
        (round(stops[1][0], 6), round(stops[1][1], 6)): [service(stops[1]), target],
        (round(stops[2][0], 6), round(stops[2][1], 6)): [service(stops[2])],
    }
    r = snap_stops_to_main_road(
        route_id="t-name-noname", stops=stops,
        overpass_fn=_overpass_stub_factory(scn),
        require_continuity=True,
    )
    assert r.snaps[1].moved is False
    assert r.snaps[1].reason == "target_has_no_name"


def test_v2_named_road_gate_rejects_different_avenue():
    stops = [(-0.20000, -78.50020), (-0.20000, -78.50000), (-0.20000, -78.49980)]
    # Continuity passes (target on both neighbours) BUT the neighbours' main
    # road is "Av. 10 de Agosto", target is "Av. Amazonas" → should reject.
    scn = _corridor_scenario(*stops, primary_name="Av. Amazonas",
                             candidate_on_prev=True, candidate_on_next=True,
                             neighbour_has_primary_with_name=True)
    r = snap_stops_to_main_road(
        route_id="tng-fail", stops=stops,
        overpass_fn=_overpass_stub_factory(scn),
        require_continuity=True,
        require_named_match=True,
    )
    assert r.snaps[1].moved is False
    assert r.snaps[1].reason == "gate_named_road_failed"


def test_v2_named_road_gate_accepts_when_name_matches():
    stops = [(-0.20000, -78.50020), (-0.20000, -78.50000), (-0.20000, -78.49980)]
    # Neighbours' primary is "Av. Amazonas", target is also "Av. Amazonas"
    # → name matches → snap should pass.
    target = {"way_id": 99, "highway": "primary", "name": "Av. Amazonas",
              "distance_m": 25.0,
              "geometry": [(-0.19990, -78.49985), (-0.20010, -78.49985)]}
    neighbour_primary = {"way_id": 88, "highway": "primary",
                         "name": "Av. Amazonas", "distance_m": 15.0,
                         "geometry": [(-0.19995, -78.50010), (-0.20005, -78.50010)]}
    service = lambda s: {"way_id": 1, "highway": "service", "distance_m": 2.0,
                         "geometry": [s]}
    scn = {
        (round(stops[0][0], 6), round(stops[0][1], 6)): [neighbour_primary, target, service(stops[0])],
        (round(stops[1][0], 6), round(stops[1][1], 6)): [service(stops[1]), target],
        (round(stops[2][0], 6), round(stops[2][1], 6)): [neighbour_primary, target, service(stops[2])],
    }
    r = snap_stops_to_main_road(
        route_id="tng-pass", stops=stops,
        overpass_fn=_overpass_stub_factory(scn),
        require_continuity=True,
        require_named_match=True,
    )
    assert r.snaps[1].moved is True
    assert r.snaps[1].reason == "snapped"
    assert r.snaps[1].best_highway == "primary"
