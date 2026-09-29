# views/phases/phase2/widgets/duplicate_name_collisions.py
from __future__ import annotations

from collections import defaultdict
from difflib import SequenceMatcher
import re
import unicodedata
from typing import Any, Dict, List, Tuple

import pandas as pd
import pydeck as pdk
import streamlit as st


def _soft_norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", str(text or ""))
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.lower()
    t = re.sub(r"[^a-z0-9\s]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _token_sort_key(text: str) -> str:
    parts = [p for p in str(text or "").split(" ") if p]
    parts.sort()
    return " ".join(parts)


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    s1 = SequenceMatcher(None, a, b).ratio()
    s2 = SequenceMatcher(None, _token_sort_key(a), _token_sort_key(b)).ratio()
    if a in b or b in a:
        return max(s1, s2, 0.9 if abs(len(a) - len(b)) <= 4 else 0.0)
    return max(s1, s2)


def _palette(i: int) -> List[int]:
    colors = [
        [30, 136, 229, 190],
        [0, 172, 193, 190],
        [67, 160, 71, 190],
        [255, 167, 38, 190],
        [141, 110, 99, 190],
        [92, 107, 192, 190],
        [0, 121, 107, 190],
        [216, 27, 96, 190],
    ]
    return colors[i % len(colors)]


def _build_groups(
    rows: List[Dict[str, Any]],
    *,
    enable_fuzzy: bool,
    threshold: float,
    max_names_for_fuzzy: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    by_norm: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        alias = str(r.get("alias") or "").strip()
        alias_norm = _soft_norm(str(r.get("alias_norm") or alias))
        pid = str(r.get("place_candidate_id") or "")
        if not alias_norm or not pid:
            continue
        item = by_norm.setdefault(
            alias_norm,
            {
                "alias_norm": alias_norm,
                "aliases": set(),
                "place_ids": set(),
                "rows": [],
            },
        )
        item["aliases"].add(alias)
        item["place_ids"].add(pid)
        item["rows"].append(r)

    keys = list(by_norm.keys())
    if not keys:
        return [], by_norm

    parent = {k: k for k in keys}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra = find(a)
        rb = find(b)
        if ra != rb:
            parent[rb] = ra

    # Always keep exact normalized duplicates as their own groups.
    # Optional fuzzy pass links close normalized forms.
    fuzzy_edges: List[Tuple[str, str, float]] = []
    if enable_fuzzy:
        scored_keys = sorted(keys, key=lambda k: (len(by_norm[k]["place_ids"]), len(by_norm[k]["rows"])), reverse=True)
        fuzzy_keys = scored_keys[: int(max(50, max_names_for_fuzzy))]
        for i in range(len(fuzzy_keys)):
            a = fuzzy_keys[i]
            for j in range(i + 1, len(fuzzy_keys)):
                b = fuzzy_keys[j]
                if a[0:1] != b[0:1]:
                    continue
                if abs(len(a) - len(b)) > 6:
                    continue
                sim = _similarity(a, b)
                if sim >= threshold:
                    union(a, b)
                    fuzzy_edges.append((a, b, sim))

    by_root: Dict[str, List[str]] = defaultdict(list)
    for k in keys:
        by_root[find(k)].append(k)

    max_edge_by_root: Dict[str, float] = {}
    for a, b, sim in fuzzy_edges:
        root = find(a)
        prev = float(max_edge_by_root.get(root) or 0.0)
        if sim > prev:
            max_edge_by_root[root] = sim

    groups: List[Dict[str, Any]] = []
    for root, norms in by_root.items():
        place_ids = set()
        aliases = set()
        for nm in norms:
            place_ids.update(by_norm[nm]["place_ids"])
            aliases.update(by_norm[nm]["aliases"])
        if len(place_ids) < 2:
            continue
        kind = "fuzzy" if len(norms) > 1 else "exact"
        group_aliases = sorted([a for a in aliases if a])[:6]
        groups.append(
            {
                "group_key": root,
                "kind": kind,
                "n_places": int(len(place_ids)),
                "n_alias_norms": int(len(norms)),
                "alias_norms": sorted(norms),
                "aliases_preview": " | ".join(group_aliases) if group_aliases else root,
                "max_similarity": float(max_edge_by_root.get(root) or 1.0 if kind == "exact" else 0.0),
            }
        )

    groups.sort(key=lambda g: (int(g["n_places"]), int(g["n_alias_norms"])), reverse=True)
    return groups, by_norm


def _render_group_map(points: List[Dict[str, Any]], *, height: int = 520) -> None:
    if not points:
        st.info("No mapped points found for this group.")
        return
    lats = [float(r["lat"]) for r in points if r.get("lat") is not None]
    lons = [float(r["lon"]) for r in points if r.get("lon") is not None]
    if not lats or not lons:
        st.info("Points missing lat/lon.")
        return
    c_lat = sum(lats) / len(lats)
    c_lon = sum(lons) / len(lons)

    layer = pdk.Layer(
        "ScatterplotLayer",
        data=points,
        get_position="[lon, lat]",
        get_fill_color="color",
        get_radius="radius",
        radius_min_pixels=3,
        radius_max_pixels=12,
        pickable=True,
        auto_highlight=True,
    )
    deck = pdk.Deck(
        layers=[layer],
        initial_view_state=pdk.ViewState(latitude=c_lat, longitude=c_lon, zoom=14, pitch=0),
        tooltip={
            "html": (
                "<b>{proposed_canonical_name}</b><br/>"
                "place_candidate_id: {place_candidate_id}<br/>"
                "node_id: {node_id}<br/>"
                "conf: {confidence}<br/>"
                "lat,lon: {lat}, {lon}"
            )
        },
        map_style=None,
    )
    st.pydeck_chart(deck, use_container_width=True, height=height)


def render_duplicate_name_collisions(
    client: Any,
    *,
    place_set_id: str,
    key_prefix: str = "p2_dupnames",
) -> None:
    st.subheader("Duplicate name collisions (exact + near)")
    st.caption(
        "Names are normalized case/accent/punctuation-insensitive. "
        "Optional fuzzy mode groups near-matches so you can review and clean duplicates."
    )

    if not place_set_id:
        st.info("Select a place_set_id first.")
        return

    ctrl1, ctrl2, ctrl3 = st.columns([1.1, 1.0, 1.0])
    with ctrl1:
        detect_mode = st.selectbox(
            "Detection mode",
            options=["Exact normalized only", "Exact + near (fuzzy)"],
            index=1,
            key=f"{key_prefix}:mode",
        )
    with ctrl2:
        fuzzy_threshold = st.slider(
            "Fuzzy similarity",
            min_value=0.70,
            max_value=0.99,
            value=0.88,
            step=0.01,
            key=f"{key_prefix}:threshold",
        )
    with ctrl3:
        max_fuzzy = st.number_input(
            "Max names for fuzzy compare",
            min_value=100,
            max_value=5000,
            value=700,
            step=100,
            key=f"{key_prefix}:max_fuzzy",
        )

    try:
        rows: List[Dict[str, Any]] = client.list_alias_collision_rows(place_set_id, limit=50000)
    except Exception as e:
        st.error(f"Failed to load collision rows: {e}")
        return

    if not rows:
        st.success("No alias rows available for this place_set.")
        return

    groups, by_norm = _build_groups(
        rows,
        enable_fuzzy=(detect_mode == "Exact + near (fuzzy)"),
        threshold=float(fuzzy_threshold),
        max_names_for_fuzzy=int(max_fuzzy),
    )

    if not groups:
        st.success("No duplicate/near-duplicate groups found.")
        return

    n_exact = sum(1 for g in groups if str(g.get("kind")) == "exact")
    n_fuzzy = sum(1 for g in groups if str(g.get("kind")) == "fuzzy")
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Groups", len(groups))
    k2.metric("Exact groups", n_exact)
    k3.metric("Fuzzy groups", n_fuzzy)
    k4.metric("Alias rows", len(rows))

    table_rows: List[Dict[str, Any]] = []
    for g in groups:
        table_rows.append(
            {
                "group": str(g.get("group_key") or "")[:48],
                "kind": g.get("kind"),
                "n_places": int(g.get("n_places") or 0),
                "n_names": int(g.get("n_alias_norms") or 0),
                "max_similarity": round(float(g.get("max_similarity") or 0.0), 3),
                "preview": g.get("aliases_preview"),
            }
        )
    st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)

    options = [
        f"[{g['kind']}] {str(g['aliases_preview'])[:65]} | places:{g['n_places']} | names:{g['n_alias_norms']}"
        for g in groups
    ]
    pick_label = st.selectbox("Open group", options=options, index=0, key=f"{key_prefix}:group_pick")
    pick_ix = options.index(pick_label)
    group = groups[pick_ix]
    norm_keys = list(group.get("alias_norms") or [])

    group_rows: List[Dict[str, Any]] = []
    for nm in norm_keys:
        group_rows.extend(list(by_norm.get(nm, {}).get("rows") or []))

    by_place: Dict[str, Dict[str, Any]] = {}
    for r in group_rows:
        pid = str(r.get("place_candidate_id") or "")
        if not pid:
            continue
        item = by_place.setdefault(
            pid,
            {
                "place_candidate_id": pid,
                "proposed_canonical_name": str(r.get("proposed_canonical_name") or "Unnamed"),
                "proposed_place_type": str(r.get("proposed_place_type") or ""),
                "matched_aliases": set(),
                "matched_norms": set(),
                "n_nodes": int(r.get("n_nodes") or 0),
                "center_lat": r.get("center_lat"),
                "center_lon": r.get("center_lon"),
            },
        )
        item["matched_aliases"].add(str(r.get("alias") or ""))
        item["matched_norms"].add(str(r.get("alias_norm") or ""))

    place_rows: List[Dict[str, Any]] = []
    place_ids = list(by_place.keys())
    for pid in place_ids:
        it = by_place[pid]
        place_rows.append(
            {
                "place_candidate_id": pid,
                "name": it["proposed_canonical_name"],
                "type": it["proposed_place_type"],
                "n_nodes": int(it["n_nodes"] or 0),
                "matched_aliases": " | ".join(sorted([x for x in it["matched_aliases"] if x])[:6]),
                "center_lat": it.get("center_lat"),
                "center_lon": it.get("center_lon"),
            }
        )
    place_rows.sort(key=lambda x: (int(x.get("n_nodes") or 0), str(x.get("name") or "")), reverse=True)

    left, right = st.columns([1.05, 1.35], gap="large")
    with left:
        st.markdown("#### Group members")
        st.dataframe(pd.DataFrame(place_rows), use_container_width=True, hide_index=True)

        open_opts = [f"{r['name']} | {str(r['place_candidate_id'])[:8]}" for r in place_rows]
        open_map = {lbl: r["place_candidate_id"] for lbl, r in zip(open_opts, place_rows)}
        if open_opts:
            chosen_place_lbl = st.selectbox("Open candidate in Workspace Nodes", open_opts, key=f"{key_prefix}:open_place")
            if st.button("Open in Workspace Nodes", use_container_width=True, key=f"{key_prefix}:open_ws_btn"):
                st.session_state["phase2.screen"] = "Workspace nodes"
                st.session_state["phase2.place_set_id"] = str(place_set_id)
                st.session_state["phase2.ws.seed_place_candidate_id"] = str(open_map.get(chosen_place_lbl) or "")
                st.rerun()

    with right:
        st.markdown("#### Group map")
        try:
            map_rows = client.get_place_candidate_points_multi(
                place_set_id=place_set_id,
                place_candidate_ids=place_ids,
                limit=20000,
            ) or []
        except Exception as e:
            st.error(f"Failed to load group points: {e}")
            map_rows = []

        color_by_pid: Dict[str, List[int]] = {}
        for i, pid in enumerate(place_ids):
            color_by_pid[pid] = _palette(i)

        coord_idx: Dict[Tuple[float, float], Dict[str, Any]] = {}
        for p in map_rows:
            lat = p.get("lat")
            lon = p.get("lon")
            pid = str(p.get("place_candidate_id") or "")
            nid = str(p.get("node_id") or "")
            if lat is None or lon is None or not pid:
                continue
            key = (round(float(lat), 7), round(float(lon), 7))
            bucket = coord_idx.setdefault(key, {"place_ids": set(), "node_ids": set()})
            bucket["place_ids"].add(pid)
            if nid:
                bucket["node_ids"].add(nid)

        exact_same_xy = [
            {"lat": k[0], "lon": k[1], "n_places": len(v["place_ids"]), "n_nodes": len(v["node_ids"])}
            for k, v in coord_idx.items()
            if len(v["place_ids"]) > 1
        ]
        st.caption(f"Exact same lat/lon across different places: {len(exact_same_xy)}")
        if exact_same_xy:
            st.dataframe(pd.DataFrame(exact_same_xy), use_container_width=True, hide_index=True)

        plot_rows: List[Dict[str, Any]] = []
        for p in map_rows:
            lat = p.get("lat")
            lon = p.get("lon")
            pid = str(p.get("place_candidate_id") or "")
            if lat is None or lon is None or not pid:
                continue
            ck = (round(float(lat), 7), round(float(lon), 7))
            repeated = len((coord_idx.get(ck) or {}).get("place_ids") or []) > 1
            plot_rows.append(
                {
                    **p,
                    "lat": float(lat),
                    "lon": float(lon),
                    "radius": 40 if repeated else 24,
                    "color": [220, 38, 38, 220] if repeated else color_by_pid.get(pid, [31, 119, 180, 180]),
                }
            )
        _render_group_map(plot_rows, height=560)

        if map_rows:
            st.markdown("#### Node actions")
            node_opts: List[str] = []
            node_map: Dict[str, Dict[str, Any]] = {}
            for p in plot_rows:
                nid = str(p.get("node_id") or "")
                if not nid:
                    continue
                label = (
                    f"{str(p.get('proposed_canonical_name') or 'Unnamed')} | "
                    f"{nid[:8]} | {float(p.get('lat') or 0):.7f}, {float(p.get('lon') or 0):.7f}"
                )
                node_opts.append(label)
                node_map[label] = p
            node_opts = list(dict.fromkeys(node_opts))
            if node_opts:
                pick_node_lbl = st.selectbox("Pick node_id", node_opts, key=f"{key_prefix}:node_pick")
                pick_node = node_map[pick_node_lbl]
                node_id = str(pick_node.get("node_id") or "")
                loaded_node_key = f"{key_prefix}:loaded_node_for_edit"
                if st.session_state.get(loaded_node_key) != node_id:
                    st.session_state[f"{key_prefix}:move_lat"] = float(pick_node.get("lat") or 0.0)
                    st.session_state[f"{key_prefix}:move_lon"] = float(pick_node.get("lon") or 0.0)
                    st.session_state[loaded_node_key] = node_id

                d1, d2 = st.columns(2)
                with d1:
                    move_lat = st.number_input(
                        "Move lat",
                        value=float(st.session_state.get(f"{key_prefix}:move_lat", pick_node.get("lat") or 0.0)),
                        step=0.000001,
                        format="%.8f",
                        key=f"{key_prefix}:move_lat",
                    )
                with d2:
                    move_lon = st.number_input(
                        "Move lon",
                        value=float(st.session_state.get(f"{key_prefix}:move_lon", pick_node.get("lon") or 0.0)),
                        step=0.000001,
                        format="%.8f",
                        key=f"{key_prefix}:move_lon",
                    )

                b1, b2 = st.columns(2)
                with b1:
                    if st.button("Move node point", use_container_width=True, key=f"{key_prefix}:move_btn"):
                        try:
                            out = client.update_node_prod_location(node_id, lat=float(move_lat), lon=float(move_lon))
                            st.success(
                                f"Moved node {str(out.get('node_id') or node_id)[:8]} to "
                                f"{float(out.get('lat') or move_lat):.8f}, {float(out.get('lon') or move_lon):.8f}"
                            )
                            st.rerun()
                        except Exception as e:
                            st.error(f"Move failed: {e}")
                with b2:
                    run_impact_check = st.button(
                        "Check DB impact before delete",
                        use_container_width=True,
                        key=f"{key_prefix}:impact_btn",
                    )
                    if run_impact_check:
                        try:
                            impact = client.delete_node_prod_node(
                                node_id,
                                purge_phase2_mappings=True,
                                mark_routes_dirty=True,
                                clear_route_approvals=True,
                                clear_route_prod_rows=True,
                                recompute_sequences_and_valhalla=False,
                                dry_run=True,
                            )
                            st.session_state[f"{key_prefix}:last_impact"] = impact
                        except Exception as e:
                            st.error(f"Impact check failed: {e}")

                    impact_obj = st.session_state.get(f"{key_prefix}:last_impact")
                    if isinstance(impact_obj, dict):
                        st.caption("Delete impact preview")
                        c_imp1, c_imp2, c_imp3, c_imp4 = st.columns(4)
                        c_imp1.metric("Impacted routes", len(impact_obj.get("impacted_route_ids") or []))
                        c_imp2.metric("Seq candidates", int(impact_obj.get("seq_candidates_with_node") or 0))
                        c_imp3.metric("Route prod rows", int(impact_obj.get("route_prod_rows_with_node") or 0))
                        c_imp4.metric("Prior matches", int(impact_obj.get("prior_matches_with_node") or 0))
                        if impact_obj.get("impacted_route_ids"):
                            st.write("Impacted route_id list")
                            st.code("\n".join([str(x) for x in impact_obj.get("impacted_route_ids")]), language="text")

                    auto_recompute = st.checkbox(
                        "After delete: recompute Step20 + Step30 for impacted routes",
                        value=True,
                        key=f"{key_prefix}:auto_recompute",
                    )
                    confirm_del = st.checkbox("Confirm delete forever", value=False, key=f"{key_prefix}:confirm_delete")
                    if st.button("Delete node forever", use_container_width=True, key=f"{key_prefix}:delete_btn"):
                        if not confirm_del:
                            st.warning("Enable confirmation checkbox first.")
                        else:
                            try:
                                out = client.delete_node_prod_node(
                                    node_id,
                                    purge_phase2_mappings=True,
                                    mark_routes_dirty=True,
                                    clear_route_approvals=True,
                                    clear_route_prod_rows=True,
                                    recompute_sequences_and_valhalla=bool(auto_recompute),
                                    dry_run=False,
                                )
                                st.success(
                                    "Deleted node "
                                    f"{node_id[:8]} | prod_map={int(out.get('deleted_prod_mappings') or 0)} | "
                                    f"work_map={int(out.get('deleted_work_mappings') or 0)} | "
                                    f"dirty_routes={int(out.get('marked_routes_dirty') or 0)}"
                                )
                                if out.get("recompute"):
                                    rows = []
                                    for rr in list(out.get("recompute") or []):
                                        rows.append(
                                            {
                                                "route_id": rr.get("route_id"),
                                                "step20_ok": bool(rr.get("step20_ok")),
                                                "step30_ok": bool(rr.get("step30_ok")),
                                                "error": rr.get("error"),
                                            }
                                        )
                                    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
                                st.rerun()
                            except Exception as e:
                                st.error(f"Delete failed: {e}")
