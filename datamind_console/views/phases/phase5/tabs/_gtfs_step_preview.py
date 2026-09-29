from __future__ import annotations

from typing import Any, Iterable, List, Optional, Sequence

import streamlit as st


GTFS_PREVIEW_TABLES: List[str] = [
    "gtfs_agency",
    "gtfs_routes",
    "gtfs_stops",
    "gtfs_shapes",
    "gtfs_calendar",
    "gtfs_calendar_dates",
    "gtfs_trips",
    "gtfs_stop_times",
    "gtfs_frequencies",
]

_HIDE_PREVIEW_COLS = {"export_run_id"}


def _dedup_rows(rows: Sequence[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        key = repr(sorted(dict(r).items()))
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(r))
    return out


def _load_rows_for_table(
    *,
    client: Any,
    table_name: str,
    export_run_id: str,
    route_ids: Sequence[str],
    limit: int,
) -> list[dict]:
    scoped_ids = [str(x).strip() for x in (route_ids or []) if str(x).strip()]
    if not scoped_ids:
        return list(client.list_gtfs_rows(table_name, export_run_id, route_id=None, limit=int(limit)) or [])
    if len(scoped_ids) == 1:
        return list(client.list_gtfs_rows(table_name, export_run_id, route_id=scoped_ids[0], limit=int(limit)) or [])

    # Multi-route scope (e.g. service_route_id with both directions).
    # Load per route and merge so route-aware joins in client are used correctly.
    per_route_limit = max(50, int(limit))
    merged: list[dict] = []
    for rid in scoped_ids:
        rows = client.list_gtfs_rows(table_name, export_run_id, route_id=rid, limit=per_route_limit) or []
        merged.extend([dict(x) for x in rows])
        if len(merged) >= int(limit):
            break
    return _dedup_rows(merged)[: int(limit)]


def render_gtfs_step_output_preview(
    *,
    client: Any,
    export_run_id: str,
    key: str,
    route_id: Optional[str] = None,
    route_ids: Optional[Iterable[str]] = None,
    title: str = "GTFS rows preview for this step",
    default_tables: Optional[Sequence[str]] = None,
    expanded: bool = False,
    note: Optional[str] = None,
) -> None:
    run_id = str(export_run_id or "").strip()
    if not run_id:
        return

    route_scope = [str(x).strip() for x in (route_ids or []) if str(x).strip()]
    if not route_scope and str(route_id or "").strip():
        route_scope = [str(route_id).strip()]

    defaults = [t for t in (default_tables or []) if t in GTFS_PREVIEW_TABLES] or GTFS_PREVIEW_TABLES[:3]
    state_key_rows = f"{key}.rows_by_table"
    state_key_meta = f"{key}.meta"

    with st.expander(title, expanded=expanded):
        c1, c2 = st.columns([2, 1])
        with c1:
            tables = st.multiselect(
                "GTFS tables to preview",
                GTFS_PREVIEW_TABLES,
                default=defaults,
                key=f"{key}.tables",
            )
        with c2:
            limit = st.number_input(
                "Rows per table",
                min_value=20,
                max_value=5000,
                value=200,
                step=20,
                key=f"{key}.limit",
            )

        if route_scope:
            st.caption(
                "Route-scoped preview: " + ", ".join([f"`{r[:8]}`" for r in route_scope])
            )
        else:
            st.caption("Export-wide preview (no route filter).")
        if note:
            st.caption(note)

        l1, l2 = st.columns([1, 1])
        with l1:
            do_load = st.button("Load step GTFS preview", use_container_width=True, key=f"{key}.load")
        with l2:
            if st.button("Clear preview cache", use_container_width=True, key=f"{key}.clear"):
                st.session_state.pop(state_key_rows, None)
                st.session_state.pop(state_key_meta, None)
                st.rerun()

        current_meta = dict(st.session_state.get(state_key_meta) or {})
        should_reload = False
        if do_load:
            should_reload = True
        elif state_key_rows not in st.session_state:
            st.info("Click `Load step GTFS preview` to visualize inserted columns/rows.")
        else:
            # If user changed table selection/limit, require explicit reload for predictable performance.
            pass

        if should_reload:
            rows_by_table: dict[str, list[dict]] = {}
            errors: dict[str, str] = {}
            for t in tables:
                try:
                    rows_by_table[t] = _load_rows_for_table(
                        client=client,
                        table_name=t,
                        export_run_id=run_id,
                        route_ids=route_scope,
                        limit=int(limit),
                    )
                except Exception as e:
                    rows_by_table[t] = []
                    errors[t] = str(e)
            st.session_state[state_key_rows] = rows_by_table
            st.session_state[state_key_meta] = {
                "run_id": run_id,
                "tables": list(tables),
                "limit": int(limit),
                "route_scope": list(route_scope),
                "errors": errors,
            }
            current_meta = dict(st.session_state.get(state_key_meta) or {})

        rows_by_table = dict(st.session_state.get(state_key_rows) or {})
        if not rows_by_table:
            return

        meta_tables = current_meta.get("tables") or list(rows_by_table.keys())
        errors = dict(current_meta.get("errors") or {})
        st.caption(
            f"Loaded preview for selected gtfs_id internal build context | limit/table={int(current_meta.get('limit') or limit)}"
        )
        if errors:
            st.warning(
                "Some tables failed to load: "
                + "; ".join([f"{k}: {v}" for k, v in errors.items()])
            )

        for t in meta_tables:
            rows = list(rows_by_table.get(t) or [])
            st.markdown(f"**{t}**")
            if rows:
                cleaned_rows = [
                    {k: v for k, v in dict(r).items() if k not in _HIDE_PREVIEW_COLS}
                    for r in rows
                ]
                cols = list(cleaned_rows[0].keys()) if cleaned_rows else []
                st.caption(f"rows loaded: {len(rows)} | columns: {', '.join(cols)}")
                st.dataframe(cleaned_rows, use_container_width=True, hide_index=True, height=220)
            else:
                st.info(f"No rows found in `{t}` for current preview scope.")
