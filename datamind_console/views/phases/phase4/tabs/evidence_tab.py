from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import streamlit as st

from datamind_console.phases.phase4_naming.client import _get_phase4_client

Json = Dict[str, Any]


@dataclass
class Phase4Context:
    user: Optional[dict] = None


def _ss():
    return st.session_state


def _ensure_phase4_state() -> None:
    ss = _ss()
    ss.setdefault("phase4.route_id", "")
    ss.setdefault("phase4.record_id", "")
    ss.setdefault("phase4.candidates", None)
    ss.setdefault("phase4.compiled_semantics", None)
    ss.setdefault("phase4.match_confidence", None)


def _clear_downstream_from_evidence_change() -> None:
    ss = _ss()
    ss["phase4.candidates"] = None
    ss["phase4.compiled_semantics"] = None
    ss["phase4.match_confidence"] = None


def _client():
    return _get_phase4_client()


def render_evidence_tab(ctx: Phase4Context) -> None:
    _ensure_phase4_state()
    ss = _ss()
    cli = _client()

    st.subheader("Phase 4 — Evidence")

    route_id = (ss.get("phase4.route_id") or "").strip()
    if not route_id:
        st.warning("Select a route in **Queue** first.")
        return

    st.success(f"Active route_id: {route_id}")

    # Quick actions
    colA, colB, colC = st.columns([1, 1, 2])
    with colA:
        if st.button("Seed from Overpass", use_container_width=True):
            try:
                out = cli.overpass_seed_for_route(route_id, limit_relations=10)
                st.toast("Seed evidence created", icon="✅")
                ss["phase4.last_seed"] = out
                _clear_downstream_from_evidence_change()
            except Exception as e:
                st.error(f"Overpass seed failed: {e}")

    with colB:
        if st.button("Refresh", use_container_width=True):
            st.rerun()

    with colC:
        st.caption("Evidence is what justifies naming. Seed + manual records live here.")

    st.divider()

    # List evidence records (global). In your API it’s not route-filtered; we show list and you pick one.
    left, right = st.columns([2, 1])

    with left:
        col1, col2, col3 = st.columns([1, 1, 1])
        with col1:
            status = st.selectbox("Status", options=["", "pending", "linked", "rejected"], index=0)
        with col2:
            limit = st.number_input("Limit", min_value=10, max_value=200, value=50, step=10)
        with col3:
            offset = st.number_input("Offset", min_value=0, max_value=10000, value=0, step=50)

        try:
            data = cli.evidence_list(limit=int(limit), offset=int(offset), status=status or None)
        except Exception as e:
            st.error(f"Failed to load evidence records: {e}")
            return

        items = data.get("items") if isinstance(data, dict) else None
        if not items:
            st.info("No evidence records.")
        else:
            for rec in items:
                rid = str(rec.get("record_id", rec.get("id", "")))
                title = rec.get("title") or rec.get("source_type") or rid
                operator = rec.get("operator") or ""
                ref = rec.get("route_ref") or ""
                src = rec.get("source_type") or rec.get("source") or ""

                with st.container(border=True):
                    st.markdown(f"**{title}**")
                    bits = []
                    if src:
                        bits.append(f"src: {src}")
                    if operator:
                        bits.append(f"operator: {operator}")
                    if ref:
                        bits.append(f"ref: {ref}")
                    if bits:
                        st.caption(" • ".join(bits))

                    cols = st.columns([1, 1, 1])
                    with cols[0]:
                        if st.button("Select", key=f"p4_ev_sel_{rid}", use_container_width=True):
                            ss["phase4.record_id"] = rid
                            _clear_downstream_from_evidence_change()
                            st.rerun()
                    with cols[1]:
                        if st.button("View", key=f"p4_ev_view_{rid}", use_container_width=True):
                            ss["phase4.record_id"] = rid
                            st.rerun()
                    with cols[2]:
                        st.code(rid, language="text")

    with right:
        st.markdown("### Create manual evidence")
        source_type = st.text_input("source_type", value="manual_admin_note")
        title = st.text_input("title", placeholder="e.g., Official schedule PDF – operator name")
        operator = st.text_input("operator", placeholder="e.g., Cooperativa X")
        route_ref = st.text_input("route_ref", placeholder="e.g., E1")
        from_name = st.text_input("from_name", placeholder="Origin")
        to_name = st.text_input("to_name", placeholder="Destination")
        confidence_hint = st.slider("confidence_hint", 0.0, 1.0, 0.65, 0.05)

        raw_text = st.text_area("raw.note", placeholder="Paste short notes / excerpt (no huge blobs).", height=120)

        if st.button("Create evidence record", use_container_width=True):
            try:
                out = cli.evidence_create(
                    source_type=source_type.strip() or "manual_admin_note",
                    title=title or None,
                    operator=operator or None,
                    route_ref=route_ref or None,
                    from_name=from_name or None,
                    to_name=to_name or None,
                    geometry_wkt=None,
                    bbox=None,
                    raw={"note": raw_text} if raw_text.strip() else {},
                    confidence_hint=float(confidence_hint),
                )
                st.success("Evidence created.")
                ss["phase4.record_id"] = str(out.get("record_id", out.get("id", ""))) or ss["phase4.record_id"]
                _clear_downstream_from_evidence_change()
                st.rerun()
            except Exception as e:
                st.error(f"Create evidence failed: {e}")

    st.divider()

    # Evidence detail (selected)
    record_id = (ss.get("phase4.record_id") or "").strip()
    if not record_id:
        st.info("Select an evidence record to view details.")
        return

    st.markdown("### Selected evidence record")
    try:
        rec = cli.evidence_get(record_id)
    except Exception as e:
        st.error(f"Failed to load evidence record: {e}")
        return

    st.code(record_id, language="text")
    st.json(rec)
