from __future__ import annotations

from typing import Any

import streamlit as st

from ._gtfs_step_preview import render_gtfs_step_output_preview


def _shape_rows_for_scope(
    shape_rows: list[dict],
    *,
    route_id: str | None,
    service_route_id: str | None,
    direction_id: int | None,
) -> list[dict]:
    rid = str(route_id or "").strip()
    srid = str(service_route_id or "").strip()
    if not rid and not srid:
        return [dict(r) for r in (shape_rows or [])]
    prefixes = []
    if srid:
        prefixes.append(f"shape_{srid}_")
    if rid:
        prefixes.append(f"shape_{rid[:8]}_")  # backward compatibility for older shape ids
    out = [
        dict(r)
        for r in (shape_rows or [])
        if any(str((r or {}).get("shape_id") or "").startswith(pfx) for pfx in prefixes)
    ]
    if direction_id is None:
        return out
    want = f"_d{int(direction_id)}"
    return [r for r in out if str(r.get("shape_id") or "").endswith(want)]


def render_step03b_shapes_tab(*, ctx: Any, client: Any, **_) -> None:
    st.markdown("### Step 02b Insert shapes (direction-aware)")
    run_id = (st.session_state.get("phase5.export_run_id") or "").strip()
    route_id = (st.session_state.get("phase5.route_id") or "").strip()
    service_route_id = (st.session_state.get("phase5.service_route_id") or "").strip()
    selected_direction = int(st.session_state.get("phase5.direction_id") or 0)

    if not run_id:
        st.info("Create/select gtfs_id first (internal export context is created automatically).")
        return

    st.caption(
        f"Context: service_route_id=`{service_route_id or '-'}` | route_id=`{route_id or '-'}` | direction_id=`{selected_direction}`"
    )
    st.caption("Shapes are generated per route+direction. Direction 1 uses reversed geometry order by default.")

    mode = st.radio(
        "Shape insert mode",
        options=[
            "Selected route + selected direction",
            "Selected route (all active directions)",
            "All routes (all active directions)",
        ],
        horizontal=True,
        key="p5.step02b.shapes.mode",
    )

    if st.button("Run Step 02b (insert shapes)", type="primary", use_container_width=True, key="p5.step02b.shapes.run"):
        try:
            if mode == "Selected route + selected direction":
                if not route_id:
                    raise RuntimeError("Select a route in Route scope selector first.")
                out = client.run_step_03b_shapes(run_id, route_id=route_id, direction_id=selected_direction)
            elif mode == "Selected route (all active directions)":
                if not route_id:
                    raise RuntimeError("Select a route in Route scope selector first.")
                out = client.run_step_03b_shapes(run_id, route_id=route_id)
            else:
                out = client.run_step_03b_shapes(run_id)
        except Exception as e:
            st.error(str(e))
        else:
            st.success("Step 02b completed.")
            st.json(out)

    st.divider()
    preview_cap = int(
        st.number_input(
            "Shapes preview row cap",
            min_value=500,
            max_value=100000,
            value=12000,
            step=500,
            key="p5.step02b.shapes.preview_cap",
            help="UI preview/counters are based on rows loaded here. JSON run result shows the true inserted total.",
        )
    )
    try:
        all_shape_rows = client.list_gtfs_rows("gtfs_shapes", run_id, route_id=None, limit=int(preview_cap)) or []
    except Exception as e:
        st.error(f"Failed to load shapes preview: {e}")
        all_shape_rows = []

    preview_scope_route_id = None
    preview_scope_dir = None
    if mode == "Selected route + selected direction":
        preview_scope_route_id = route_id or None
        preview_scope_dir = selected_direction
    elif mode == "Selected route (all active directions)":
        preview_scope_route_id = route_id or None
    scoped_shape_rows = _shape_rows_for_scope(
        all_shape_rows,
        route_id=preview_scope_route_id,
        service_route_id=(service_route_id or None),
        direction_id=preview_scope_dir,
    )

    shape_ids = sorted({str(r.get("shape_id") or "") for r in scoped_shape_rows if str(r.get("shape_id") or "").strip()})
    total_shape_counts = {}
    try:
        total_shape_counts = client.get_gtfs_shape_counts(run_id) or {}
    except Exception as e:
        st.caption(f"Total shapes count unavailable: {e}")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("shape rows shown (preview)", len(scoped_shape_rows))
    c2.metric("shapes in preview", len(shape_ids))
    c3.metric("shape rows total", int(total_shape_counts.get("shape_rows") or 0))
    c4.metric("shapes total", int(total_shape_counts.get("shape_ids") or 0))
    if len(all_shape_rows) >= preview_cap:
        st.caption(
            f"Preview is capped at {preview_cap:,} shape-point rows. "
            "The JSON result from Step 02b reports the actual inserted total."
        )

    if shape_ids:
        st.caption("Generated shapes in current preview scope")
        st.dataframe([{"shape_id": s} for s in shape_ids[:500]], use_container_width=True, hide_index=True, height=160)
    else:
        st.info("No shapes generated yet for current preview scope.")

    if scoped_shape_rows:
        show_rows = [
            {
                "shape_id": r.get("shape_id"),
                "shape_pt_sequence": r.get("shape_pt_sequence"),
                "shape_pt_lat": r.get("shape_pt_lat"),
                "shape_pt_lon": r.get("shape_pt_lon"),
                "shape_dist_traveled": r.get("shape_dist_traveled"),
            }
            for r in scoped_shape_rows[:600]
        ]
        st.caption("Shape points preview")
        st.dataframe(show_rows, use_container_width=True, hide_index=True, height=220)

    render_gtfs_step_output_preview(
        client=client,
        export_run_id=run_id,
        key="p5.step03b.preview",
        route_id=(route_id or None),
        title="Step 02b GTFS output preview (shapes)",
        default_tables=["gtfs_shapes"],
        note="Direction-aware shapes use `shape_<service_route_id>_d<direction_id>` (fallback legacy format may appear for older rows).",
    )
