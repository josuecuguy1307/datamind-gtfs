from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import streamlit as st

# ✅ ABSOLUTE IMPORT — in-process client
from datamind_console.phases.phase4_naming.client import _get_phase4_client

Json = Dict[str, Any]


@dataclass
class Phase4Context:
    user: Optional[dict] = None


def _ss():
    return st.session_state


def _ensure_state():
    ss = _ss()
    ss.setdefault("phase4.route_id", "")
    ss.setdefault("phase4.record_id", "")
    ss.setdefault("phase4.candidates", None)
    ss.setdefault("phase4.match_confidence", None)


# ✅ SINGLE SOURCE OF TRUTH
def _client():
    return _get_phase4_client()


def _score_to_confidence(items: list[dict]) -> float:
    if not items:
        return 0.0
    s1 = float(items[0].get("score", 0.0) or 0.0)
    s2 = float(items[1].get("score", 0.0) or 0.0) if len(items) > 1 else 0.0
    gap = max(0.0, s1 - s2)
    return max(0.0, min(1.0, 0.55 + 0.45 * (gap / (abs(s1) + 1e-6))))


def render_matching_tab(ctx: Phase4Context) -> None:
    _ensure_state()
    ss = _ss()
    cli = _client()

    st.subheader("Phase 4 — Matching")

    route_id = (ss.get("phase4.route_id") or "").strip()
    record_id = (ss.get("phase4.record_id") or "").strip()

    if not route_id:
        st.warning("Select a route in **Queue** first.")
        return
    if not record_id:
        st.warning("Select an evidence record in **Evidence** first.")
        return

    st.success(f"Active route_id: {route_id}")
    st.info(f"Evidence record_id: {record_id}")

    colA, colB, colC = st.columns([1, 1, 2])
    with colA:
        limit = st.number_input("Top-K", min_value=5, max_value=100, value=25, step=5)
    with colB:
        mode = st.selectbox(
            "Mode",
            ["heuristic_candidates", "model_predict_and_log"],
            index=1,
        )
    with colC:
        st.caption("Ranks candidate routes for the selected evidence record.")

    st.divider()

    if st.button("Run matching", use_container_width=True):
        try:
            if mode == "heuristic_candidates":
                out = cli.match_candidates(
                    record_id=record_id,
                    limit=int(limit),
                )
            else:
                out = cli.match_predict_and_log(
                    record_id=record_id,
                    model_name="lgbm_lambdarank",
                    model_version="v1",
                    limit=int(limit),
                )

            ss["phase4.candidates"] = out
            items = out.get("items") if isinstance(out, dict) else []
            ss["phase4.match_confidence"] = (
                _score_to_confidence(items) if isinstance(items, list) else None
            )

            st.toast("Matching complete", icon="✅")
        except Exception as e:
            st.error(f"Matching failed: {e}")

    st.divider()

    out = ss.get("phase4.candidates")
    if not out:
        st.info("Run matching to see candidates.")
        return

    items = out.get("items")
    if not items:
        st.warning("No candidates returned.")
        return

    conf = ss.get("phase4.match_confidence")
    if conf is not None:
        st.metric("Match confidence (heuristic)", f"{conf:.2f}")

    st.markdown("### Ranked candidates")

    for i, cand in enumerate(items[: int(limit)]):
        cid = str(cand.get("route_id", ""))
        score = cand.get("score")
        title = cand.get("route_name") or cand.get("name") or cid

        with st.container(border=True):
            cols = st.columns([4, 1, 1, 1])

            with cols[0]:
                st.markdown(f"**#{i+1} {title}**")
                st.caption(f"route_id: {cid} • score: {score}")
                if cand.get("features"):
                    with st.expander("features"):
                        st.json(cand["features"])

            with cols[1]:
                if cid == route_id:
                    st.write("✅")

            with cols[2]:
                rel = st.selectbox(
                    "Label",
                    options=[0, 1, 2],
                    index=2 if cid == route_id else 0,
                    key=f"p4_label_{record_id}_{cid}",
                )

            with cols[3]:
                if st.button(
                    "Save",
                    key=f"p4_save_label_{record_id}_{cid}",
                    use_container_width=True,
                ):
                    try:
                        cli.match_label(
                            record_id=record_id,
                            route_id=cid,
                            relevance=int(rel),
                            notes=None,
                            label_source="admin_ui",
                        )
                        st.toast("Label saved", icon="✅")
                    except Exception as e:
                        st.error(f"Label save failed: {e}")
