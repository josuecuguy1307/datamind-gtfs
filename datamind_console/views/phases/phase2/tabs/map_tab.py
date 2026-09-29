from __future__ import annotations

from typing import Any, Dict, List, Optional, Callable, Tuple

import streamlit as st

from ui.deck.render import render_deck
from ui.deck.viewstate import viewstate_from_points
from ui.deck.layers.phase2_layers import build_phase2_layers


def _safe_list(x: Any) -> list:
    return x if isinstance(x, list) else []


def _pick_first_callable(obj: Any, names: List[str]) -> Optional[Callable[..., Any]]:
    for n in names:
        fn = getattr(obj, n, None)
        if callable(fn):
            return fn
    return None


def _parse_bbox_center(bbox: Any) -> Optional[Tuple[float, float]]:
    """
    Accepts:
      - string: "min_lat,min_lon,max_lat,max_lon"
      - string: "-0.35,-78.6,-0.05,-78.35"
      - list/tuple: [min_lat, min_lon, max_lat, max_lon]
      Returns: (lat_center, lon_center)
    """
    if bbox is None:
        return None

    try:
        if isinstance(bbox, str):
            parts = [float(p.strip()) for p in bbox.split(",")]
        elif isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            parts = [float(p) for p in bbox]
        else:
            return None

        min_lat, min_lon, max_lat, max_lon = parts
        return ((min_lat + max_lat) / 2.0, (min_lon + max_lon) / 2.0)
    except Exception:
        return None


def _point_has_coords(p: Dict[str, Any]) -> bool:
    lat = p.get("lat", p.get("latitude"))
    lon = p.get("lon", p.get("lng", p.get("longitude")))
    return lat is not None and lon is not None


def render_map_tab(*, ctx: Any, phase2: Any = None, **_) -> None:
    st.subheader("Phase 2 — Map")

    phase2 = phase2 or getattr(ctx, "phase2", None)
    if phase2 is None:
        st.error("Phase2 client not available (ctx.phase2 missing).")
        return

    ss = st.session_state
    context_key = ss.get("phase2.context_key") or getattr(ctx, "workspace_context_key", None) or "default"
    limit_default = int(ss.get("phase2.limit", 300))

    # --- UI controls
    colA, colB, colC = st.columns([1.2, 1.2, 0.9])
    with colA:
        show_compare = st.toggle("Compare candidates vs accepted", value=True)
    with colB:
        only_with_coords = st.toggle("Only points with coords", value=True)
    with colC:
        limit = st.number_input("Limit", min_value=50, max_value=5000, value=max(50, limit_default), step=50)

    min_score = st.slider("Min score", 0.0, 1.0, 0.0, 0.01)
    active_only = st.toggle("Active only", value=bool(ss.get("phase2.active_only", True)))

    # ------------------------------------------------------------
    # ✅ Fetch candidates:
    # Prefer point-level method if it exists; else fallback to sets.
    # ------------------------------------------------------------
    list_candidates_fn = _pick_first_callable(
        phase2,
        ["list_candidates", "list_candidate_places", "list_place_candidates", "candidates_list"],
    )

    list_candidate_sets_fn = _pick_first_callable(
        phase2,
        ["list_candidate_sets", "list_place_sets", "candidate_sets_list"],
    )

    candidates: List[Dict[str, Any]] = []

    if list_candidates_fn is not None:
        # Point-level (best)
        try:
            candidates = _safe_list(
                list_candidates_fn(
                    context_key=context_key,
                    place_set_id=ss.get("phase2.place_set_id"),
                    extract_run_id=ss.get("phase2.extract_run_id"),
                    active_only=active_only,
                    limit=int(limit),
                )
            )
        except TypeError:
            # Client doesn't accept kwargs
            candidates = _safe_list(list_candidates_fn())
    elif list_candidate_sets_fn is not None:
        # Fallback: plot sets as points (centroids) so map works NOW
        try:
            sets = _safe_list(list_candidate_sets_fn(context_key=context_key, limit=int(limit)))
        except TypeError:
            sets = _safe_list(list_candidate_sets_fn())

        # Optional: let user pick a set ID if you want (uses your workspace field)
        # If you already type it in workspace, you can skip this block.
        if sets and not ss.get("phase2.place_set_id"):
            options = [s.get("place_set_id") or s.get("id") for s in sets]
            options = [o for o in options if o is not None]
            if options:
                chosen = st.selectbox("Pick Place Set ID", options=options, index=0)
                ss["phase2.place_set_id"] = chosen

        for s in sets:
            if not isinstance(s, dict):
                continue

            # Coordinates:
            lat = s.get("lat", s.get("latitude"))
            lon = s.get("lon", s.get("lng", s.get("longitude")))

            if lat is None or lon is None:
                center = _parse_bbox_center(s.get("bbox") or s.get("bounds"))
                if center:
                    lat, lon = center

            candidates.append(
                {
                    "name": s.get("name") or s.get("label") or str(s.get("place_set_id") or s.get("id") or "candidate_set"),
                    "score": float(s.get("score", s.get("mean_score", 1.0)) or 1.0),
                    "source": "candidate_set",
                    "lat": lat,
                    "lon": lon,
                    # Keep id fields for tooltip/debug
                    "place_set_id": s.get("place_set_id") or s.get("id"),
                    "bbox": s.get("bbox") or s.get("bounds"),
                }
            )
    else:
        st.error(
            "Phase2 client has no known way to list candidates.\n\n"
            "Expected one of: list_candidates(...) OR list_candidate_sets(...)."
        )
        with st.expander("Debug: client methods"):
            st.write(sorted([m for m in dir(phase2) if not m.startswith("_")]))
        return

    # ------------------------------------------------------------
    # ✅ Fetch accepted (optional)
    # If you don't have accepted yet, it will just be empty.
    # ------------------------------------------------------------
    accepted: List[Dict[str, Any]] = []

    if show_compare:
        list_accepted_fn = _pick_first_callable(
            phase2,
            ["list_accepted", "list_accepted_places", "accepted_list", "list_prod_places"],
        )
        if list_accepted_fn is not None:
            try:
                accepted = _safe_list(
                    list_accepted_fn(
                        context_key=context_key,
                        place_set_id=ss.get("phase2.place_set_id"),
                        active_only=active_only,
                        limit=int(limit),
                    )
                )
            except TypeError:
                accepted = _safe_list(list_accepted_fn())

    # --- Filter: coords + min_score
    def _ok(p: Dict[str, Any]) -> bool:
        try:
            s = p.get("score")
            if s is not None and float(s) < float(min_score):
                return False
        except Exception:
            pass
        if only_with_coords and not _point_has_coords(p):
            return False
        return True

    candidates = [p for p in candidates if isinstance(p, dict) and _ok(p)]
    accepted = [p for p in accepted if isinstance(p, dict) and _ok(p)]

    if not candidates and not accepted:
        st.info("No map points found for the current filters/selection.")
        return

    # --- Build + render layers
    layers = build_phase2_layers(
        candidates=candidates,
        accepted=accepted,
        show_compare=show_compare,
    )

    view_state = viewstate_from_points(candidates or accepted)

    tooltip = {"html": "<b>{name}</b><br/>score: {score}<br/>source: {source}"}

    render_deck(layers=layers, view_state=view_state, tooltip=tooltip)

