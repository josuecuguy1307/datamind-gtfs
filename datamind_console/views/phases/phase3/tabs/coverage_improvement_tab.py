"""Phase 3 — Coverage Improvement tab.

Sister to ``re_entry_review_tab``. Focused on the follow-up workflow:
filter pending re-entry v2 proposals by ``stop_coverage_report.classification``
(``good`` / ``acceptable`` / ``degraded`` / ``unroutable``), surface the gap
details + Tier 4 DR landmark coverage per route, and hand the operator a
per-row "what would unlock this" diagnostic.

This is the visualization layer for skill
``16_POST_ENHANCE_DR_TRIAGE.md``'s triage manifest. It does NOT perform
the approval action (that still lives in ``re_entry_review_tab``) — it
provides the *coverage-quality* slice so the operator can decide what
needs a third-pass DR vs what is already max-covered.

Three sections stack top-to-bottom:

1. **Summary header** — counts by classification (all pending QC v2s).
2. **Filter + table** — one row per v2, sortable/filterable.
3. **Per-row drill-down** — expandable. Shows gap list, which gaps have
   Tier 4 landmarks, which don't, and which DR batch file covers them
   (or which third-pass batch file now queries the still-uncovered ones).
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from glob import glob
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import psycopg2
import psycopg2.extras
import pydeck as pdk
import streamlit as st

from hades.enforcers import re_entry_swap_service as swap_svc

from datamind_console.db.db import active_dsn, read_only_connection
from datamind_console.i18n import t
from datamind_console.ui.components.states import error_state


ROOT = Path(__file__).resolve().parents[5]

_COV_LABELS = ("good", "acceptable", "degraded", "unroutable")
# Badge per coverage class; the explanation is coverage.cov_class.<class> in the catalog.
_COV_BADGE = {
    "good":       ("🟢", "good"),
    "acceptable": ("🟡", "acceptable"),
    "degraded":   ("🟠", "degraded"),
    "unroutable": ("🔴", "unroutable"),
}


def _dsn() -> str:
    """Canonical active DSN (honours local-only mode); never displayed."""
    return active_dsn()


@contextmanager
def _connect():
    """Short-lived READ ONLY connection, always closed (canonical helper)."""
    with read_only_connection(statement_timeout_ms=30_000) as conn:
        yield conn


def _load_validated_landmarks() -> dict[str, set[int]]:
    """Union of all validated/*.json files into {route_code: {gap_idx, …}}.

    gap_number in the validator output is 1-indexed; we store 0-indexed
    gap_idx here (matches what the Fixer consumes).
    """
    out: dict[str, set[int]] = {}
    base = ROOT / "workspace" / "dr_stop_coverage" / "validated"
    for p in sorted(base.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        for bucket in ("accepted", "accept_uncertain", "uncertain"):
            for item in (data.get(bucket) or []):
                rc = item.get("route_code")
                gnum = item.get("gap_number")
                if not rc or gnum is None:
                    continue
                out.setdefault(rc, set()).add(int(gnum) - 1)
    return out


@st.cache_data(ttl=120.0)
def _load_qc_pending_v2s(unit: str) -> pd.DataFrame:
    """Pull every pending QC (or whichever unit) v2 proposal + its coverage metrics."""
    # Resolve unit → route_ids from the assignment map
    rid_path = ROOT / "workspace" / "unit_logs" / "_assignment_maps" / f"{unit}.json"
    if not rid_path.exists():
        return pd.DataFrame()
    rids = json.loads(rid_path.read_text()).get("all_route_ids") or []
    if not rids:
        return pd.DataFrame()

    sql = """
    SELECT aq.queue_id::text                                    AS queue_id,
           aq.route_code::text                                  AS route_id,
           aq.version                                           AS proposed_version,
           aq.enqueued_at,
           r.route_name,
           r.province,
           r.source                                             AS source,
           COALESCE(array_length(r.stop_node_ids,1), 0)          AS v1_stops,
           aq.policy_flags,
           aq.stop_coverage_report,
           aq.geometry_report,
           aq.proposed_shape,
           aq.proposed_stops,
           aq.dr_queries_queued,
           aq.dr_queries_deferred,
           aq.quality_class,
           aq.tier4_pending_count,
           aq.pending_dr_batches,
           aq.classified_at,
           aq.reclassified_count
      FROM route_prod.approval_queue aq
      JOIN route_prod.routes r ON r.route_id::text = aq.route_code::text
     WHERE aq.status = 'pending'
       AND aq.policy_profile IN ('conservative', 'balanced')
       AND aq.policy_flags ? 're_entry_queue_id'
       AND aq.route_code::text = ANY(%s)
     ORDER BY aq.stop_coverage_report->>'classification' DESC,
              aq.enqueued_at DESC
    """
    with _connect() as conn, conn.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cur:
        cur.execute(sql, (rids,))
        rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        return pd.DataFrame()

    validated = _load_validated_landmarks()

    records = []
    for r in rows:
        sc = r.get("stop_coverage_report") or {}
        cov_class = sc.get("classification")
        gaps = sc.get("gaps") or []
        unres = [g for g in gaps if not (g.get("resolution") or {}).get("resolved")]
        unres_idxs = {int(g.get("idx", -1)) for g in unres}
        route_cov = validated.get(r["route_id"], set())
        still_uncov = unres_idxs - route_cov

        flags = r.get("policy_flags") or {}
        fix_cat = flags.get("fix_category")
        v1_n_stops = flags.get("v1_n_stops") or r.get("v1_stops") or 0
        v2_n_stops = flags.get("v2_n_stops") or 0

        records.append({
            "queue_id": r["queue_id"],
            "route_id": r["route_id"],
            "short_id": r["route_id"][:8],
            "route_name": r.get("route_name"),
            "source": (r.get("source") or "")[:45],
            "cov_class": cov_class,
            "cov_badge": _COV_BADGE.get(cov_class, ("❓", ""))[0],
            "quality_class": r.get("quality_class") or "unknown",
            "tier4_pending_count": int(r.get("tier4_pending_count") or 0),
            "pending_dr_batches": list(r.get("pending_dr_batches") or []),
            "classified_at": r.get("classified_at"),
            "fix_category": fix_cat,
            "n_stops_v1": v1_n_stops,
            "n_stops_v2": v2_n_stops,
            "n_gaps_total": len(gaps),
            "n_gaps_unresolved": len(unres_idxs),
            "n_gaps_with_tier4": len(unres_idxs & route_cov),
            "n_gaps_needing_dr": len(still_uncov),
            "dr_queued": len(r.get("dr_queries_queued") or []),
            "dr_deferred": len(r.get("dr_queries_deferred") or []),
            "proposed_shape": r.get("proposed_shape"),
            "proposed_stops": r.get("proposed_stops"),
            "gaps_detail": gaps,
            "unresolved_idxs": sorted(unres_idxs),
            "tier4_covered_idxs": sorted(unres_idxs & route_cov),
            "still_uncovered_idxs": sorted(still_uncov),
        })
    return pd.DataFrame.from_records(records)


def _render_route_map(row: pd.Series) -> None:
    shape = row.get("proposed_shape")
    stops = row.get("proposed_stops") or []
    if isinstance(shape, str):
        try: shape = json.loads(shape)
        except Exception: shape = None
    if not shape or not isinstance(shape, dict):
        st.info(t("ui.p3.coverage_improvement.no_v2_geometry_available"))
        return
    coords = shape.get("coordinates") or []
    if not coords:
        st.info(t("ui.p3.coverage_improvement.no_coords_v2_geometry"))
        return
    mid_lon, mid_lat = coords[len(coords) // 2][:2]

    path_data = [{"name": "v2", "path": [[c[0], c[1]] for c in coords]}]
    layers = [
        pdk.Layer(
            "PathLayer",
            data=path_data,
            get_path="path",
            get_width=5,
            width_min_pixels=3,
            get_color=[255, 90, 95, 220],
        ),
    ]
    # Stops (differentiate synthetic / dr_landmark / original)
    stops_df = pd.DataFrame([
        {
            "lon": float(s.get("lon") or 0),
            "lat": float(s.get("lat") or 0),
            "label": str(s.get("stop_id") or ""),
            "kind": ("fixer" if (str(s.get("stop_id") or "").startswith("fix_gap"))
                     else ("synthesized" if (str(s.get("stop_id") or "").startswith("synthesized_"))
                           else "original")),
        }
        for s in stops if s.get("lat") is not None and s.get("lon") is not None
    ])
    if not stops_df.empty:
        color_map = {
            "original":     [90, 90, 90, 200],
            "synthesized":  [255, 180, 0, 220],
            "fixer":        [255, 90, 95, 240],
        }
        stops_df["color"] = stops_df["kind"].map(color_map)
        layers.append(pdk.Layer(
            "ScatterplotLayer",
            data=stops_df,
            get_position="[lon, lat]",
            get_radius=30,
            radius_min_pixels=4,
            get_fill_color="color",
        ))

    st.pydeck_chart(
        pdk.Deck(
            map_style="https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json",
            initial_view_state=pdk.ViewState(
                latitude=float(mid_lat),
                longitude=float(mid_lon),
                zoom=12,
                pitch=0,
            ),
            layers=layers,
            tooltip={"text": "{label} ({kind})"},
        ),
        use_container_width=True,
    )
    st.caption(
        t("ui.p3.coverage_improvement.legend_grey_v1_original_stops_amber")
    )


def _gap_table(row: pd.Series) -> pd.DataFrame:
    rows = []
    tier4_set = set(row.get("tier4_covered_idxs") or [])
    for g in (row.get("gaps_detail") or []):
        idx = int(g.get("idx", -1))
        res = (g.get("resolution") or {}) or {}
        rows.append({
            "gap_idx": idx,
            "gap_m": int(g.get("gap_m") or 0),
            "zone": g.get("zone"),
            "resolved": bool(res.get("resolved")),
            "tier": res.get("tier") or "",
            "tier_label": res.get("tier_label") or "",
            "has_tier4_landmark": (not res.get("resolved")) and (idx in tier4_set),
        })
    return pd.DataFrame(rows)


def _operator_identity() -> tuple[Optional[str], str]:
    user = st.session_state.get("auth.user") or {}
    operator_id = user.get("user_id")
    operator_username = (
        user.get("display_name")
        or user.get("email")
        or str(st.session_state.get("auth.username") or "operator")
    )
    return operator_id, operator_username


def _load_snapper_eval_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _snapper_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    n = len(rows)
    moved = sum(1 for r in rows if r.get("n_moved", 0) > 0)
    wins = sum(1 for r in rows if r.get("verdict") == "win" or
               (isinstance(r.get("class_change"), str) and r["class_change"].startswith("upgrade")))
    regs = sum(1 for r in rows if r.get("verdict") == "regression" or
               (isinstance(r.get("class_change"), str) and r["class_change"].startswith("downgrade")))
    rev = sum(1 for r in rows if r.get("verdict") == "reverted_length_gate")
    deltas = [r.get("shape_length_delta_m", 0.0) for r in rows
              if r.get("retraced") and "shape_length_delta_m" in r]
    p50 = sorted(deltas)[len(deltas) // 2] if deltas else 0.0
    p99 = sorted(deltas)[max(0, int(0.99 * (len(deltas) - 1)))] if deltas else 0.0
    return {
        "n": n, "moved": moved, "wins": wins, "regressions": regs,
        "reverted_length": rev,
        "delta_p50_m": round(p50, 1) if deltas else None,
        "delta_p99_m": round(p99, 1) if deltas else None,
    }


# ---------------------------------------------------------------------------
# v2 quality-class buckets — same taxonomy as re_entry_review_tab.
# ---------------------------------------------------------------------------

_Q_BUCKETS = {
    "ready_to_ship": ("good", "acceptable", "ship_pending_dr"),
    "review_first":  ("degraded_minor",),
    "needs_work":    ("degraded",),
    "cannot_fix":    ("unroutable",),
}
_Q_BADGE = {
    "ready_to_ship": "🟢",
    "review_first":  "🟡",
    "needs_work":    "🟠",
    "cannot_fix":    "🔴",
}


def _render_dr_enrichment_summary(unit: str) -> None:
    """DR enrichment progress for this unit: rounds of prompts + responses +
    landmarks landed + processed/ archive count. Shows the operator at a
    glance how much DR work has been done and how much remains.
    """
    base = ROOT / "workspace/dr_stop_coverage"
    pending = base / "pending_requests" / unit
    processed = pending / "processed"
    responses = base / "responses" / unit
    validated = base / "validated"

    # Count prompt files in each stage — glob only unit-prefixed .md
    # (skip INSTRUCTIONS siblings).
    def _count_prompts(d: Path) -> int:
        if not d.exists(): return 0
        return sum(1 for p in d.glob("*.md")
                   if not p.name.endswith(".INSTRUCTIONS.md")
                   and p.name != "QC_MAPPING.md")

    n_pending  = _count_prompts(pending)
    n_archived = _count_prompts(processed)
    n_total    = n_pending + n_archived
    if n_total == 0:
        return

    # Count saved responses for this unit
    n_responses = 0
    if responses.exists():
        n_responses = sum(1 for p in responses.glob("*.md"))

    # Validator totals per-unit (accepted + uncertain landmarks)
    n_accepted = 0
    n_uncertain = 0
    n_rejected = 0
    if validated.exists():
        for vf in validated.glob("*.json"):
            try:
                d = json.loads(vf.read_text())
            except Exception:
                continue
            # Only count rows whose route_code is in this unit — quick heuristic:
            # the validator stamps batch_file name, and qc_*.json / qn_*.json etc.
            # correspond to units via prefix. For now count all since each unit
            # dashboard is focused.
            counts = d.get("counts") or {}
            # Scope by whether any bucket has a route for this unit.
            # Filter via assignment map.
        # Quick scoped count: intersect accepted/uncertain route_codes
        # with unit assignment.
        rid_path = ROOT / "workspace/unit_logs/_assignment_maps" / f"{unit}.json"
        unit_rids = set(json.loads(rid_path.read_text()).get("all_route_ids", []) or []) if rid_path.exists() else set()
        for vf in validated.glob("*.json"):
            try:
                d = json.loads(vf.read_text())
            except Exception:
                continue
            for r in (d.get("accepted") or []):
                if r.get("route_code") in unit_rids: n_accepted += 1
            for r in (d.get("accept_uncertain") or []):
                if r.get("route_code") in unit_rids: n_uncertain += 1
            for r in (d.get("rejected") or []):
                if r.get("route_code") in unit_rids: n_rejected += 1

    st.markdown("---")
    st.markdown(t("ui.p3.coverage_improvement.dr_enrichment_cumulative_results"))
    cols = st.columns(5)
    cols[0].metric(t("ui.p3.coverage_improvement.prompts_total"), n_total,
                   delta=t("coverage.dr.pending_processed", pending=n_pending, processed=n_archived),
                   delta_color="off")
    cols[1].metric(t("ui.p3.coverage_improvement.responses_saved"), n_responses)
    cols[2].metric(t("ui.p3.coverage_improvement.accept_landmarks"), n_accepted)
    cols[3].metric(t("ui.p3.coverage_improvement.uncertain"), n_uncertain)
    cols[4].metric(t("ui.p3.coverage_improvement.reject"), n_rejected,
                   delta=t("coverage.dr.accept_rate", pct=f"{100*n_accepted/max(n_accepted+n_uncertain+n_rejected,1):.0f}"),
                   delta_color="off")


def _render_quality_buckets(df: pd.DataFrame) -> None:
    """4-bucket header + DR-batches-blocking aggregation."""
    if df.empty or "quality_class" not in df.columns:
        return
    by_class = df["quality_class"].fillna("unknown").value_counts().to_dict()
    last_classified = df["classified_at"].dropna().max() if "classified_at" in df.columns else None
    st.markdown(
        t("ui.p3.coverage_improvement.n_df_pending_last_reclassified_last", n_df=(len(df)), last_classified=(last_classified or '(never)'))
    )
    cols = st.columns(len(_Q_BUCKETS))
    for i, (name, classes) in enumerate(_Q_BUCKETS.items()):
        n = sum(int(by_class.get(c, 0)) for c in classes)
        breakdown = " · ".join(f"{c}: {int(by_class.get(c, 0))}" for c in classes)
        cols[i].metric(f"{_Q_BADGE[name]} {t(f'reentry.bucket.{name}')}", n, delta=breakdown,
                       delta_color="off")

    blocking: dict[str, int] = {}
    for batches in df["pending_dr_batches"].fillna("").tolist() if "pending_dr_batches" in df.columns else []:
        for b in (batches or []):
            blocking[b] = blocking.get(b, 0) + 1
    if blocking:
        with st.expander(
            t("ui.p3.coverage_improvement.dr_batches_blocking_shipments_n_blocking", n_blocking=(len(blocking))),
            expanded=False,
        ):
            st.dataframe(
                pd.DataFrame(
                    sorted(blocking.items(), key=lambda kv: -kv[1]),
                    columns=["dr_batch_id", "routes_waiting"],
                ),
                hide_index=True, use_container_width=True,
            )


def _render_dr_dependency_panel_for_row(row: pd.Series) -> None:
    """Per-route DR-dependency block (drilldown view)."""
    cls = row.get("quality_class")
    pending = list(row.get("pending_dr_batches") or [])
    if cls not in ("ship_pending_dr", "degraded_minor") or not pending:
        return
    st.markdown(t("ui.p3.coverage_improvement.dr_dependencies"))
    st.markdown(
        t("ui.p3.coverage_improvement.waiting_n_pending_dr_batch_es", n_pending=(len(pending)), tier4_pending_count=(int(row.get('tier4_pending_count') or 0)))
    )
    for b in pending:
        if b.startswith("batch_"):
            link = f"workspace/dr_stop_coverage/queries/{b}.md"
            note = t("coverage.dr.existing_batch")
        else:
            link = f"workspace/dr_stop_coverage/pending_requests/{b}.md"
            note = t("coverage.dr.on_demand")
        st.markdown(f"- `{b}` → `{link}`  {note}")


# ---------------------------------------------------------------------------
# DR Type 1 (route grounding) status — for routes with n_stops=0 the
# coverage-improvement tab needs to surface them too, since DR Type 2
# cannot help them and they sit in `unroutable` until Type 1 lands.
# ---------------------------------------------------------------------------

def _load_type1_prompt_status(unit: str) -> dict[str, dict[str, Any]]:
    """Scan workspace/research_queue/{pending,sent,responses}/<unit>/ and
    return ``{route_code: {status, prompt_path, response_path}}``.

    Status one of:
        ``pending``    — prompt file under pending/, no response yet
        ``sent``       — file moved to sent/ (operator submitted to Claude.ai)
        ``responded``  — JSON file exists under responses/

    Returns an empty dict if the workspace folders don't exist.
    """
    base = ROOT / "workspace" / "research_queue"
    pending_dir = base / "pending" / unit
    sent_dir    = base / "sent" / unit
    responses_dir = base / "responses" / unit
    if not (pending_dir.exists() or sent_dir.exists() or responses_dir.exists()):
        return {}

    out: dict[str, dict[str, Any]] = {}

    def _route_from_prompt(p: Path) -> Optional[str]:
        # Front-matter line `route_code: "<uuid>"` — robust to quotes.
        try:
            for line in p.read_text(errors="ignore").splitlines()[:30]:
                if line.startswith("route_code:"):
                    return line.split(":", 1)[1].strip().strip('"').strip("'")
        except OSError:
            return None
        return None

    for label, d in (("pending", pending_dir), ("sent", sent_dir)):
        if not d.exists():
            continue
        for p in d.glob("01_grounding_*.md"):
            rc = _route_from_prompt(p)
            if rc is None:
                continue
            out[rc] = {
                "status": label,
                "prompt_path": str(p.relative_to(ROOT)),
                "response_path": None,
            }
    if responses_dir.exists():
        # Content-based match: scan ALL .json responses, peek at the
        # route_code field, and mark its prompt status as 'responded'
        # regardless of filename. The operator sometimes saves responses
        # with non-canonical names (qc_NN_routename.json, dup-suffixed
        # "(1)", short-slug variants) — content match is robust to that.
        for p in sorted(responses_dir.glob("*.json")):
            try:
                data = json.loads(p.read_text())
            except Exception:
                continue
            rc = data.get("route_code") or (data.get("metadata") or {}).get("route_code")
            if rc and rc in out:
                # Don't overwrite an earlier responded entry — first match wins.
                if out[rc]["status"] != "responded":
                    out[rc]["status"] = "responded"
                    out[rc]["response_path"] = str(p.relative_to(ROOT))
    return out


def _render_type1_grounding_status(unit: str) -> None:
    """Per-unit DR Type 1 prompt tracker — surfaces n_stops=0 cohort progress."""
    status = _load_type1_prompt_status(unit)
    if not status:
        return
    by_status = {"pending": 0, "sent": 0, "responded": 0}
    for v in status.values():
        by_status[v["status"]] = by_status.get(v["status"], 0) + 1
    st.markdown("---")
    st.markdown(t("ui.p3.coverage_improvement.dr_type_1_route_grounding_n"))
    st.caption(
        t("ui.p3.coverage_improvement.routes_that_dr_type_2_cannot")
    )
    cols = st.columns(3)
    cols[0].metric(t("ui.p3.coverage_improvement.pending_not_yet_pasted"), by_status.get("pending", 0))
    cols[1].metric(t("ui.p3.coverage_improvement.sent_claude_ai"),      by_status.get("sent", 0))
    cols[2].metric(t("ui.p3.coverage_improvement.responded"),                by_status.get("responded", 0))
    rows = []
    for rc, v in sorted(status.items()):
        rows.append({
            "route": rc[:8],
            "status": v["status"],
            "prompt": v["prompt_path"].rsplit("/", 1)[-1] if v["prompt_path"] else "",
            "response": v["response_path"].rsplit("/", 1)[-1] if v["response_path"] else "—",
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


# ---------------------------------------------------------------------------
# Failed re_entry_queue routes — visible so the operator knows what's stuck.
# ---------------------------------------------------------------------------

def _load_failed_re_entry(unit: str) -> pd.DataFrame:
    """Pull re_entry_queue rows in status='failed' OR 'quarantined' that
    represent **actually actionable** routes — i.e. the operator could
    do something about them.

    Filters out:
    - Routes that already have a pending / approved v2 somewhere
      (historical noise from reject + re-run cycles).
    - `classification=unclassified — no playbook` rows (out-of-scope:
      these source_types are not handled by the re-entry pipeline,
      they need a separate workflow).

    What remains: the cohort the operator needs to triage manually
    or re-run with different config.
    """
    rid_path = ROOT / "workspace" / "unit_logs" / "_assignment_maps" / f"{unit}.json"
    if not rid_path.exists():
        return pd.DataFrame()
    rids = json.loads(rid_path.read_text()).get("all_route_ids") or []
    if not rids:
        return pd.DataFrame()
    sql = """
      SELECT q.route_id::text  AS route_code,
             q.status,
             q.attempts,
             q.attempted_at,
             q.last_error,
             r.route_name
        FROM route_prod.re_entry_queue q
        LEFT JOIN route_prod.routes r ON r.route_id = q.route_id
       WHERE q.status IN ('failed', 'quarantined')
         AND q.route_id::text = ANY(%s)
         AND NOT EXISTS (
             SELECT 1 FROM route_prod.approval_queue aq
              WHERE aq.route_code = q.route_id::text
                AND aq.status IN ('pending', 'approved')
         )
         AND COALESCE(q.last_error, '') NOT LIKE 'classification=unclassified%%'
         AND COALESCE(q.last_error, '') NOT LIKE '[superseded%%'
         AND q.attempted_at >= NOW() - INTERVAL '48 hours'
       ORDER BY q.attempted_at DESC NULLS LAST
    """
    with _connect() as conn, conn.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cur:
        cur.execute(sql, (rids,))
        return pd.DataFrame.from_records([dict(r) for r in cur.fetchall()])


def _render_failed_re_entry_panel(unit: str) -> None:
    """Actionable-failed cohort summary. Noisy historical rows are filtered
    out in the loader; here we further group by error family so the
    operator sees a compact summary instead of a long table."""
    df = _load_failed_re_entry(unit)
    if df.empty:
        return

    # Bucket error messages into families.
    def _bucket(err: str) -> str:
        err = (err or "").lower()
        if "would_regress" in err: return "would_regress"
        if "already_good"  in err: return "already_good"
        if "scratch_no_improvement" in err: return "scratch_no_improvement"
        if "scratch_built" in err: return "scratch_built"
        if "coordinator rejected" in err: return "coordinator_reject"
        if "no_improvement" in err: return "no_improvement"
        if not err.strip(): return "empty"
        return "other"

    df = df.copy()
    df["route"] = df["route_code"].str[:8]
    df["error_bucket"] = df["last_error"].fillna("").apply(lambda e: t(f"coverage.error_family.{_bucket(e)}"))
    summary = df["error_bucket"].value_counts().to_dict()

    st.markdown("---")
    st.markdown(
        t("ui.p3.coverage_improvement.recent_failed_re_entry_queue_n", n_df=(len(df)))
    )
    st.caption(
        t("ui.p3.coverage_improvement.scoped_last_48_h_excludes_unclassified")
    )
    # Summary by error family
    summary_rows = [{t("coverage.col.error_family"): k, t("coverage.col.routes"): v} for k, v in
                    sorted(summary.items(), key=lambda kv: -kv[1])]
    st.dataframe(pd.DataFrame(summary_rows), hide_index=True, use_container_width=True)

    with st.expander(t("ui.p3.coverage_improvement.see_all_n_df_routes", n_df=(len(df)))):
        df_disp = df[["route", "route_name", "status", "attempts",
                      "attempted_at", "error_bucket", "last_error"]].copy()
        df_disp["last_error"] = df_disp["last_error"].fillna("").str.slice(0, 140)
        df_disp = df_disp.rename(columns={
            "route": t("col.route"), "route_name": t("coverage.col.name"), "status": t("coverage.col.status"),
            "attempts": t("coverage.col.attempts"), "attempted_at": t("coverage.col.attempted_at"),
            "error_bucket": t("coverage.col.error_family"), "last_error": t("coverage.col.last_error"),
        })
        st.dataframe(df_disp, hide_index=True, use_container_width=True, height=360)


def _render_latest_pass_summary(unit: str) -> None:
    """Read the most recent COMPLETE.md from the unit's run logs and surface
    the headline numbers, so the operator can see at a glance what state the
    pending queue arrived from."""
    st.markdown("---")
    st.markdown(t("ui.p3.coverage_improvement.latest_re_entry_pass_summary"))
    base = ROOT / "workspace" / "unit_logs" / unit
    cmpl = base / "COMPLETE.md"
    if not cmpl.exists():
        st.info(t("ui.p3.coverage_improvement.no_complete_md_yet_unit_unit", unit=(unit)))
        return
    text = cmpl.read_text()
    # Extract a few headline lines
    headline_keys = [
        "Total routes processed", "v2_ready", "failed",
        "Avg time per route", "Total elapsed", "policy_profile", "dr_landmarks_glob",
    ]
    summary_lines = []
    for line in text.splitlines():
        if any(k in line for k in headline_keys):
            summary_lines.append(line.strip("- "))
    st.markdown("\n".join(f"- {l}" for l in summary_lines[:10]))
    with st.expander(t("ui.p3.coverage_improvement.full_complete_md")):
        st.markdown(text)


def _render_snapper_experiments(unit: str) -> None:
    """[DEPRECATED 2026-04-22] Snapper / sequence-optimizer experiment summary.
    Kept behind an expander so future operators can see what was tried and why
    it was reverted; not surfaced by default."""
    base = ROOT / "workspace" / "unit_logs" / unit
    has_any_snapper = (
        (base / "snapper_eval" / "per_route.jsonl").exists()
        or (base / "snapper_eval_v2" / "per_route.jsonl").exists()
        or (base / "snapper_eval_v2b" / "per_route.jsonl").exists()
    )
    if not has_any_snapper:
        return
    with st.expander(
        t("ui.p3.coverage_improvement.archived_experiments_snapper_sequence_optimizer_deprecated"),
        expanded=False,
    ):
        st.warning(
            t("ui.p3.coverage_improvement.these_experiments_archived_all_three_iterations")
        )

        v1_rows = _load_snapper_eval_jsonl(base / "snapper_eval" / "per_route.jsonl")
        v2_rows = _load_snapper_eval_jsonl(base / "snapper_eval_v2" / "per_route.jsonl")
        v2b_rows = _load_snapper_eval_jsonl(base / "snapper_eval_v2b" / "per_route.jsonl")

        if not (v1_rows or v2_rows or v2b_rows):
            return

        cmp = pd.DataFrame.from_records([
            {"variant": "v1 (no gates)", **_snapper_summary(v1_rows)},
            {"variant": "v2 (way_id continuity + name + length)", **_snapper_summary(v2_rows)},
            {"variant": "v2b (way_NAME continuity + name + length)", **_snapper_summary(v2b_rows)},
        ]).rename(columns={
            "variant": t("coverage.col.variant"),
            "n": t("coverage.col.routes"),
            "moved": t("coverage.col.moved"),
            "wins": t("coverage.col.wins"),
            "regressions": t("coverage.col.regressions"),
            "reverted_length": t("coverage.col.reverted_length"),
            "delta_p50_m": t("coverage.col.delta_p50"),
            "delta_p99_m": t("coverage.col.delta_p99"),
        })
        st.dataframe(cmp, use_container_width=True, hide_index=True)
        st.caption(
            t("ui.p3.coverage_improvement.v1_8_wins_16_regressions_detours")
        )


def render_coverage_improvement_tab(ctx=None, **deps) -> None:
    top_l, top_r = st.columns([6, 1])
    with top_l:
        st.subheader(t("ui.p3.coverage_improvement.coverage_improvement_queue"))
        st.caption(
            t("ui.p3.coverage_improvement.every_pending_re_entry_v2_classified")
        )
    with top_r:
        if st.button(t("ui.p3.coverage_improvement.refresh"), key="p3.cov_improve.refresh", use_container_width=True):
            _load_qc_pending_v2s.clear()  # type: ignore[attr-defined]
            st.rerun()

    # Unit selector — auto-discovered from workspace/unit_logs/_assignment_maps/*.json
    _amap_dir = ROOT / "workspace" / "unit_logs" / "_assignment_maps"
    _unit_options = sorted(
        p.stem for p in _amap_dir.glob("*.json") if p.stem != "unclassified"
    )
    if not _unit_options:
        st.info(t("ui.p3.coverage_improvement.no_regional_unit_selected_create_select"))
        return
    _default_idx = 0
    unit = st.selectbox(
        t("ui.p3.coverage_improvement.unit"),
        options=_unit_options,
        index=_default_idx,
        key="p3.cov_improve.unit",
    )

    _render_latest_pass_summary(unit)

    df = _load_qc_pending_v2s(unit)
    if not df.empty:
        st.markdown("---")
        st.markdown(t("ui.p3.coverage_improvement.v2_quality_buckets"))
        _render_quality_buckets(df)

    # DR enrichment cumulative results — shows how much DR work has been
    # done for this unit (prompts generated, responses saved, landmarks
    # accepted/rejected). Read-only, recomputed each render.
    _render_dr_enrichment_summary(unit)

    # DR Type 1 + failed-route panels surface routes that don't appear in the
    # main pending v2 cohort (Type 1 routes have n_stops=0 and may not have
    # an approval_queue row; failed routes were rejected by the worker).
    _render_type1_grounding_status(unit)
    _render_failed_re_entry_panel(unit)

    if df.empty:
        st.info(t("ui.p3.coverage_improvement.no_pending_re_entry_v2_proposals", unit=(unit)))
        _render_snapper_experiments(unit)
        return

    # Summary counts — uses quality_class (the live v2 taxonomy kept in
    # sync by the reclassifier). cov_class is the LEGACY field from the
    # first worker INSERT and is out of date after a DR cycle;
    # quality_class is what the rest of this tab + the dashboard
    # bucket header already reports.
    if "quality_class" in df.columns:
        c_good = int((df["quality_class"] == "good").sum())
        c_acc  = int((df["quality_class"] == "acceptable").sum())
        c_spd  = int((df["quality_class"] == "ship_pending_dr").sum())
        c_dmin = int((df["quality_class"] == "degraded_minor").sum())
        c_deg  = int((df["quality_class"] == "degraded").sum())
        c_unr  = int((df["quality_class"] == "unroutable").sum())
        total  = c_good + c_acc + c_spd + c_dmin + c_deg + c_unr

        cols = st.columns(7)
        cols[0].metric(t("ui.p3.coverage_improvement.total_pending"),   total)
        cols[1].metric(t("ui.p3.coverage_improvement.good"),         c_good)
        cols[2].metric(t("ui.p3.coverage_improvement.acceptable"),   c_acc)
        cols[3].metric(t("ui.p3.coverage_improvement.ship_pending_dr"), c_spd)
        cols[4].metric(t("ui.p3.coverage_improvement.degraded_minor"),  c_dmin)
        cols[5].metric(t("ui.p3.coverage_improvement.degraded"),     c_deg)
        cols[6].metric(t("ui.p3.coverage_improvement.unroutable"),   c_unr)
    else:
        # Fallback for older rows that never got quality_class populated.
        c_good = int((df["cov_class"] == "good").sum())
        c_acc  = int((df["cov_class"] == "acceptable").sum())
        c_deg  = int((df["cov_class"] == "degraded").sum())
        c_unr  = int((df["cov_class"] == "unroutable").sum())
        total  = c_good + c_acc + c_deg + c_unr

        cols = st.columns(5)
        cols[0].metric(t("ui.p3.coverage_improvement.total_pending"), total)
        cols[1].metric(t("ui.p3.coverage_improvement.good"),       c_good)
        cols[2].metric(t("ui.p3.coverage_improvement.acceptable_2"), c_acc)
        cols[3].metric(t("ui.p3.coverage_improvement.degraded"),   c_deg)
        cols[4].metric(t("ui.p3.coverage_improvement.unroutable"), c_unr)

    st.markdown("---")

    # Filter — uses live quality_class taxonomy (7 classes incl.
    # ship_pending_dr, degraded_minor). Falls back to cov_class if the
    # column is absent.
    _QC_CLASS_LABELS = (
        "good", "acceptable", "ship_pending_dr",
        "degraded_minor", "degraded", "unroutable",
    )
    class_col = "quality_class" if "quality_class" in df.columns else "cov_class"
    filter_choice = st.multiselect(
        t("ui.p3.coverage_improvement.show_classifications_filtering_class_col", class_col=(class_col)),
        options=list(_QC_CLASS_LABELS),
        default=["degraded_minor", "degraded", "unroutable"],
        key="p3.cov_improve.filter",
        help=(t("ui.p3.coverage_improvement.default_shows_classes_that_still_need")),
    )
    filtered = df[df[class_col].isin(filter_choice)].reset_index(drop=True)
    st.caption(
        t("ui.p3.coverage_improvement.showing_n_filtered_n_df_v2s", n_filtered=(len(filtered)), n_df=(len(df)), class_col=(class_col))
    )

    # Sort key: worst classes first, then routes with the most DR-needed gaps
    qc_order = {
        "unroutable": 0, "degraded": 1, "degraded_minor": 2,
        "ship_pending_dr": 3, "acceptable": 4, "good": 5,
    }
    filtered = filtered.assign(
        _qc_order=filtered[class_col].map(qc_order).fillna(99)
    ).sort_values(
        ["_qc_order", "n_gaps_needing_dr", "n_gaps_unresolved"],
        ascending=[True, False, False],
    ).drop(columns=["_qc_order"])

    # Summary table — compact
    display_df = filtered[[
        "cov_badge", "short_id", "route_name", "fix_category",
        "n_stops_v1", "n_stops_v2",
        "n_gaps_total", "n_gaps_unresolved",
        "n_gaps_with_tier4", "n_gaps_needing_dr",
    ]].rename(columns={
        "cov_badge": "",
        "short_id": t("col.route"),
        "route_name": t("coverage.col.name"),
        "fix_category": t("coverage.col.fix"),
        "n_stops_v1": t("coverage.col.v1_stops"),
        "n_stops_v2": t("coverage.col.v2_stops"),
        "n_gaps_total": t("coverage.col.gaps"),
        "n_gaps_unresolved": t("coverage.col.unresolved"),
        "n_gaps_with_tier4": t("coverage.col.tier4"),
        "n_gaps_needing_dr": t("coverage.col.needs_dr"),
    })
    st.dataframe(display_df, use_container_width=True, hide_index=True, height=380)

    st.markdown("---")

    # Per-row drill-down
    if len(filtered) > 0:
        choice = st.selectbox(
            t("ui.p3.coverage_improvement.drill_into_specific_route"),
            options=filtered.index.tolist(),
            format_func=lambda i: (
                f"{filtered.loc[i,'cov_badge']} {filtered.loc[i,'short_id']} "
                f"— {filtered.loc[i,'route_name']} "
                f"(unres={filtered.loc[i,'n_gaps_unresolved']}, needs_DR={filtered.loc[i,'n_gaps_needing_dr']})"
            ),
            key="p3.cov_improve.drill",
        )
        row = filtered.loc[choice]

        left, right = st.columns([2, 1])
        with right:
            st.markdown(t("ui.p3.coverage_improvement.classification"))
            badge, cov_key = _COV_BADGE.get(row["cov_class"], ("❓", ""))
            tooltip = t(f"coverage.cov_class.{cov_key}") if cov_key else ""
            st.markdown(f"### {badge}  **{row['cov_class']}**")
            st.caption(tooltip)
            st.markdown(t("ui.p3.coverage_improvement.shape_stops"))
            st.markdown(
                t("ui.p3.coverage_improvement.v1_v2_stops_n_stops_v1", n_stops_v1=(row['n_stops_v1']), n_stops_v2=(row['n_stops_v2']), fix_category=(row['fix_category'] or '(none)'), n_gaps_total=(row['n_gaps_total']), n_gaps_unresolved=(row['n_gaps_unresolved']), n_gaps_with_tier4=(row['n_gaps_with_tier4']), n_gaps_needing_dr=(row['n_gaps_needing_dr']), dr_queued=(row['dr_queued']), dr_deferred=(row['dr_deferred']))
            )
            if row["n_gaps_needing_dr"] > 0:
                st.warning(
                    t("ui.p3.coverage_improvement.n_gaps_needing_dr_gap_s", n_gaps_needing_dr=(row['n_gaps_needing_dr']))
                )
            else:
                if row["n_gaps_unresolved"] > 0:
                    st.info(
                        t("ui.p3.coverage_improvement.all_unresolved_gaps_already_have_validated")
                    )
                else:
                    st.success(t("ui.p3.coverage_improvement.no_unresolved_gaps_this_v2_fixer"))

        with left:
            st.markdown(t("ui.p3.coverage_improvement.v2_geometry_stops"))
            _render_route_map(row)
            st.markdown(t("ui.p3.coverage_improvement.gap_detail"))
            st.dataframe(_gap_table(row), use_container_width=True, hide_index=True)
            _render_dr_dependency_panel_for_row(row)

        # Action row — approve / reject / quarantine directly from this tab.
        st.markdown("---")
        st.markdown(t("ui.p3.coverage_improvement.action_this_v2"))
        operator_id, operator_username = _operator_identity()
        if operator_id is None:
            st.warning(
                t("ui.p3.coverage_improvement.no_authenticated_user_sign_from_settings")
            )
        else:
            qid = row["queue_id"]
            a1, a2, a3 = st.columns(3)
            with a1:
                do_approve = st.button(
                    t("ui.p3.coverage_improvement.approve_v2_swap_place"),
                    key=f"p3.cov_imp.approve.{qid}",
                    type="primary",
                    use_container_width=True,
                )
            with a2:
                reject_reason = st.text_input(
                    t("ui.p3.coverage_improvement.reject_reason"),
                    key=f"p3.cov_imp.reject_reason.{qid}",
                    placeholder=t("ui.p3.coverage_improvement.why_v2_worse_than_v1"),
                )
                do_reject = st.button(
                    t("ui.p3.coverage_improvement.reject_back_pending"),
                    key=f"p3.cov_imp.reject.{qid}",
                    use_container_width=True,
                )
            with a3:
                quar_reason = st.text_input(
                    t("ui.p3.coverage_improvement.quarantine_reason"),
                    key=f"p3.cov_imp.quar_reason.{qid}",
                    placeholder=t("ui.p3.coverage_improvement.needs_manual_fix"),
                )
                do_quar = st.button(
                    t("ui.p3.coverage_improvement.quarantine"),
                    key=f"p3.cov_imp.quar.{qid}",
                    use_container_width=True,
                )

            if do_approve:
                try:
                    result = swap_svc.approve_v2(
                        approval_queue_id=qid,
                        operator_id=operator_id,
                        operator_username=operator_username,
                        dsn=_dsn(),
                    )
                    st.success(
                        t("ui.p3.coverage_improvement.swap_applied_v_version_before_v", version_before=(result.version_before), version_after=(result.version_after), fix_report_id=(result.fix_report_id), n_created_synthetic_node_ids=(len(result.created_synthetic_node_ids)))
                    )
                    _load_qc_pending_v2s.clear()  # type: ignore[attr-defined]
                    st.rerun()
                except swap_svc.SwapError as exc:
                    st.error(t("ui.p3.coverage_improvement.swap_rejected_by_business_rule_exc", exc=(exc)))
                except Exception as exc:
                    error_state(exc, context_key="error.generic.title", retry=False)

            if do_reject:
                if not (reject_reason or "").strip():
                    st.error(t("ui.p3.coverage_improvement.reject_reason_required"))
                else:
                    try:
                        swap_svc.reject_v2(
                            approval_queue_id=qid,
                            operator_id=operator_id,
                            operator_username=operator_username,
                            reason=reject_reason.strip(),
                            dsn=_dsn(),
                        )
                        st.success(t("ui.p3.coverage_improvement.v2_rejected_re_entry_queue_reset"))
                        _load_qc_pending_v2s.clear()  # type: ignore[attr-defined]
                        st.rerun()
                    except swap_svc.SwapError as exc:
                        st.error(t("ui.p3.coverage_improvement.reject_rejected_by_business_rule_exc", exc=(exc)))
                    except Exception as exc:
                        error_state(exc, context_key="error.generic.title", retry=False)

            if do_quar:
                if not (quar_reason or "").strip():
                    st.error(t("ui.p3.coverage_improvement.quarantine_reason_required"))
                else:
                    try:
                        swap_svc.quarantine_v2(
                            approval_queue_id=qid,
                            operator_id=operator_id,
                            operator_username=operator_username,
                            reason=quar_reason.strip(),
                            dsn=_dsn(),
                        )
                        st.success(t("ui.p3.coverage_improvement.v2_quarantined"))
                        _load_qc_pending_v2s.clear()  # type: ignore[attr-defined]
                        st.rerun()
                    except swap_svc.SwapError as exc:
                        st.error(t("ui.p3.coverage_improvement.quarantine_rejected_by_business_rule_exc", exc=(exc)))
                    except Exception as exc:
                        error_state(exc, context_key="error.generic.title", retry=False)

    st.markdown("---")
    st.markdown(t("ui.p3.coverage_improvement.how_produce_third_pass_dr_batch"))
    st.code(
        "# 1. Re-triage the current degraded/unroutable cohort\n"
        "python3 -c '...'  # see workspace/unit_logs/<unit>/05_triage_degraded_v2.json\n\n"
        "# 2. Generate the v3 query files (renamed to avoid colliding with v2)\n"
        f"python scripts/export_uncovered_dr_batch.py --unit {unit} "
        f"--manifest workspace/unit_logs/{unit}/05_triage_degraded_v2.json\n"
        "# then rename the outputs to …_uncovered_v3_part{NN}.md before pasting.\n\n"
        "# 3. Operator pastes each into Claude.ai (web search ON), saves response\n"
        "#    to workspace/dr_stop_coverage/responses/unit_<unit>_uncovered_v3_partNN.md\n"
        "# 4. Importer + validator; new landmarks land in validated/\n"
        "# 5. Revert the affected queue rows and re-run the worker.",
        language="bash",
    )

    # Archived snapper / sequence-optimizer experiments (deprecated 2026-04-22)
    _render_snapper_experiments(unit)


__all__ = ["render_coverage_improvement_tab"]
