from __future__ import annotations

from typing import Any, List

import streamlit as st


def render_phase4_workspace_map(*, client: Any, key: str = "p4_workspace") -> None:
    ss = st.session_state

    st.markdown("#### Workspace Map")

    # Build route selector options from DB
    route_ids: List[str] = []
    try:
        pending = client.list_pending(limit=300)
        route_ids.extend([str(r.route_id) for r in pending if getattr(r, "route_id", None)])
    except Exception:
        pass

    try:
        sem_rows = client.list_semantics(limit=300)
        route_ids.extend([str(r.get("route_id")) for r in sem_rows if r.get("route_id")])
    except Exception:
        pass

    # de-duplicate while preserving order
    seen = set()
    route_ids = [x for x in route_ids if not (x in seen or seen.add(x))]

    if route_ids:
        current = (ss.get("phase4.route_id") or "").strip()
        idx = route_ids.index(current) if current in route_ids else 0
        pick = st.selectbox("route_id", route_ids, index=idx, key=f"{key}.route_id")
        ss["phase4.route_id"] = pick
    else:
        pick = (ss.get("phase4.route_id") or "").strip()
        st.caption("No route_id options available yet.")

    st.markdown("##### Naming Seed")
    try:
        seed = client.get_seed_summary(pick) if pick else {}
    except Exception:
        seed = {}

    if seed:
        seed_payload = seed.get("seed_payload") or {}
        gtfs_route = seed_payload.get("gtfs_route") if isinstance(seed_payload, dict) else {}
        gtfs_short = ""
        gtfs_long = ""
        if isinstance(gtfs_route, dict):
            gtfs_short = str(gtfs_route.get("route_short_name") or "").strip()
            gtfs_long = str(gtfs_route.get("route_long_name") or "").strip()
        st.caption(
            f"source: {seed.get('seed_source') or '-'} | "
            f"ref: {seed.get('seed_route_ref') or '-'} | "
            f"operator: {seed.get('seed_operator_name') or '-'}"
        )
        if gtfs_short or gtfs_long:
            st.caption(f"GTFS names -> short: {gtfs_short or '-'} | long: {gtfs_long or '-'}")
        st.code(
            (seed.get("seed_route_name") or "(no route name seed)") + "\n"
            + f"{seed.get('seed_from_name') or '-'} -> {seed.get('seed_to_name') or '-'}",
            language="text",
        )
    else:
        st.info("No naming seed yet. Run Step 10.")

    mode = st.selectbox(
        "Display",
        ["Route geometry", "Seed summary", "Name candidates", "Review status"],
        index=1,
        key=f"{key}.mode",
    )

    if not pick:
        st.info("Select a route_id to render workspace data.")
        return

    if mode == "Route geometry":
        # Reuse existing geometry widget
        try:
            from views.phases.phase4.widgets.route_geometry_map import render_route_geometry_map

            render_route_geometry_map(pick, key=f"{key}.geom")
        except Exception as e:
            st.error(f"Failed to render route geometry: {e}")

    elif mode == "Seed summary":
        try:
            row = client.get_seed_summary(pick)
        except Exception as e:
            st.error(f"Failed to load seed summary: {e}")
            row = {}
        if row:
            st.json(row)
        else:
            st.info("No seed summary yet. Run Step 10.")

    elif mode == "Name candidates":
        try:
            rows = client.list_name_candidates(pick)
        except Exception as e:
            st.error(f"Failed to load name candidates: {e}")
            rows = []
        if rows:
            st.dataframe(rows, use_container_width=True, height=280)
            options = [f"{r.get('candidate_id')} | {r.get('route_name')}" for r in rows]
            selected = st.selectbox("active candidate", options, key=f"{key}.active_candidate")
            st.session_state["phase4.active_candidate_id"] = selected.split("|", 1)[0].strip()
        else:
            st.info("No candidates yet. Run Step 20.")

    else:
        try:
            rows = client.list_semantics(limit=300)
            rows = [r for r in rows if str(r.get("route_id")) == pick]
        except Exception as e:
            st.error(f"Failed to load review status: {e}")
            rows = []
        if rows:
            st.dataframe(rows, use_container_width=True, height=280)
        else:
            st.info("No approved semantics row yet. Run Step 30.")
