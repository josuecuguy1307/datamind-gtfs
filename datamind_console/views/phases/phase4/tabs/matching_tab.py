"""Phase 4 · Matching: propose routes for one evidence record, then a person labels them.

*Generate candidates* stores a deterministic, explained proposal; it never
accepts anything. Each label is a human decision with its audit row. There is
no trained ranking model, so model prediction is not offered.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import streamlit as st

from datamind_console.i18n import status_label, t
from datamind_console.phases.phase4_naming import evidence_matching as em
from datamind_console.review.human_review import ReviewInputError, recent_decisions
from datamind_console.ui.components.review import current_reviewer, decision_log, flash
from datamind_console.ui.components.states import error_state
from datamind_console.views.phases.phase4.tabs.evidence_tab import error_text, schema_ready

FLASH_KEY = "p4match.flash"
RELEVANCE_OPTIONS = (2, 1, 0)


@dataclass
class Phase4Context:
    user: Optional[dict] = None


def render_matching_tab(ctx: Phase4Context) -> None:
    st.subheader(t("p4match.title"), anchor=False)
    st.caption(t("p4match.intro"))
    if not schema_ready():
        return
    flash(FLASH_KEY)
    st.info(t("p4match.no_model"), icon=":material/model_training:")

    record_id = (st.session_state.get("phase4.record_id") or "").strip()
    if not record_id:
        st.warning(t("p4match.pick_evidence"))
        return
    rec = em.evidence_detail(record_id)
    if not rec:
        st.warning(t("p4match.pick_evidence"))
        return
    _record_summary(rec)
    reviewer = current_reviewer()
    if not reviewer.name.strip():
        st.caption(t("p4match.reviewer_hint"))
    active_route = (st.session_state.get("phase4.route_id") or "").strip()

    c1, c2 = st.columns([1, 3], vertical_alignment="bottom")
    with c1:
        limit = st.number_input(t("p4match.limit"), min_value=5, max_value=100, value=25, step=5, key="p4match.limit")
    with c2:
        if st.button(t("p4match.generate"), type="primary", key="p4match.generate"):
            try:
                out = em.generate_candidates(record_id, reviewer=reviewer, limit=int(limit))
            except ReviewInputError as exc:
                st.error(error_text(str(exc)))
            except Exception as exc:  # noqa: BLE001 - rendered and logged
                error_state(exc, context_key="p4ev.error.write_failed", retry=False)
            else:
                flash(FLASH_KEY, t("p4match.generated", n=len(out["items"]), scored=out["routes_scored"]))
                st.rerun()
    st.caption(t("p4match.how"))

    candidates = em.list_candidates(record_id)
    if not candidates:
        st.info(t("p4match.no_candidates"), icon=":material/search_off:")
    for cand in candidates:
        _candidate(cand, record_id=record_id, reviewer=reviewer, active_route=active_route)

    st.markdown(f"#### {t('p4match.log.title')}")
    decision_log(recent_decisions("evidence_match"), empty_key="p4match.log.empty")


def _record_summary(rec: dict[str, Any]) -> None:
    bits = [x for x in (rec.get("route_ref"), rec.get("route_name"), rec.get("operator_name")) if x]
    ends = " → ".join(x for x in (rec.get("from_name"), rec.get("to_name")) if x)
    if ends:
        bits.append(ends)
    st.success(t("p4match.record", what=" · ".join(bits) or t("p4match.record_empty"), record=rec["record_id"][:8]),
               icon=":material/description:")


def _candidate(cand: dict[str, Any], *, record_id: str, reviewer, active_route: str) -> None:
    route_id = cand["route_id"]
    parts = (cand.get("score_parts") or {}).get("signals") or {}
    with st.container(border=True):
        head, score = st.columns([4, 1])
        with head:
            mark = " ⭐" if cand.get("is_best") else ""
            here = f" · {t('p4match.active_route')}" if route_id == active_route else ""
            st.markdown(f"**{cand.get('route_name') or route_id[:8]}**{mark}{here}")
            st.caption(" · ".join(f"{t(f'p4match.signal.{k}')} {v:.2f}" for k, v in parts.items()) or t("p4match.no_signals"))
            if cand.get("relevance") is not None:
                st.caption(t("p4match.labelled", relevance=status_label("match_relevance", cand["relevance"]),
                             who=cand.get("reviewed_by") or "—"))
        score.metric(t("p4match.score"), f"{float(cand['score']):.2f}")
        current = cand.get("relevance")
        relevance = st.segmented_control(
            t("p4match.label"), RELEVANCE_OPTIONS, key=f"p4match.rel.{record_id}.{route_id}",
            default=current if current in RELEVANCE_OPTIONS else None,
            format_func=lambda v: status_label("match_relevance", v),
        )
        n1, n2 = st.columns([3, 1], vertical_alignment="bottom")
        with n1:
            notes = st.text_input(t("p4match.notes"), key=f"p4match.notes.{record_id}.{route_id}",
                                  value=cand.get("label_notes") or "")
        with n2:
            save = st.button(t("p4match.save"), key=f"p4match.save.{record_id}.{route_id}", disabled=relevance is None,
                             width="stretch")
        if save and relevance is not None:
            try:
                em.label_match(record_id, route_id, int(relevance), reviewer=reviewer, notes=notes,
                               match_id=cand.get("match_id"))
            except ReviewInputError as exc:
                st.error(error_text(str(exc)))
            except Exception as exc:  # noqa: BLE001 - rendered and logged
                error_state(exc, context_key="p4ev.error.write_failed", retry=False)
            else:
                flash(FLASH_KEY, t("p4match.saved"))
                st.rerun()
