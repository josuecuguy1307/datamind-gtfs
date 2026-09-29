#!/usr/bin/env python3
"""
Standalone Streamlit pipeline monitoring dashboard.

Run separately from the main console:
    streamlit run phase5_gtfs/scripts/pipeline_dashboard_app.py --server.port 8503

Shows: cycle status, phase coverage, catalog gaps (06b-S + 06b-R), GTFS readiness, next actions.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="HADES Pipeline Dashboard",
    layout="wide",
    initial_sidebar_state="collapsed",
)

DB_DSN = os.environ.get(
    "DB_DSN",
    "postgresql://localhost:5432/datamind_ml",
)

# Prompt file names
PROMPT_FILES = {
    0: "PROMPT_06_ZERO.md",
    1: "PROMPT_06a_INVENTORY.md",
    "06b-S": "PROMPT_06b_S_SCHEDULES.md",
    "06b-R": "PROMPT_06b_R_RUNTIMES.md",
    3: "PROMPT_06c_FARES.md",
}

BATCH_06B_S = 18
BATCH_06B_R = 9


def _get_conn():
    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = True
    return conn


def _q(cur, sql: str) -> int:
    try:
        cur.execute(sql)
        return cur.fetchone()[0]
    except Exception:
        return 0


def _q_all(conn, sql: str) -> list:
    import psycopg2.extras
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(sql)
        return cur.fetchall()
    except Exception:
        return []


# ── Data ────────────────────────────────────────────────────

@st.cache_data(ttl=60, show_spinner=False)
def fetch_health() -> Dict[str, int]:
    conn = _get_conn()
    cur = conn.cursor()
    queries = {
        "nodes": "SELECT COUNT(*) FROM node_prod.nodes",
        "places": "SELECT COUNT(*) FROM geo_prod.places WHERE status='active'",
        "garbage_names": (
            "SELECT COUNT(*) FROM geo_prod.places WHERE status='active' "
            "AND canonical_name IN ('(sin nombre)','SN','Parada Sin Nombre',"
            "'Parada','La y','S/N','Sin Nombre','N/A')"
        ),
        "embeddings": (
            "SELECT COUNT(*) FROM geo_prod.place_embeddings pe "
            "JOIN geo_prod.places p ON p.place_id=pe.place_id WHERE p.status='active'"
        ),
        "routes": "SELECT COUNT(*) FROM route_prod.routes",
        "semantics": "SELECT COUNT(DISTINCT route_id) FROM catalog.route_semantics",
        "schedules": "SELECT COUNT(DISTINCT route_id) FROM catalog.route_schedule_profile",
        "service_days": "SELECT COUNT(DISTINCT route_id) FROM catalog.route_service_days",
        "estimates": "SELECT COUNT(DISTINCT route_id) FROM gtfs_work.runtime_route_estimates",
        "gtfs_feeds": "SELECT COUNT(*) FROM gtfs_prod.feed_versions",
        "research_runtimes": (
            "SELECT COUNT(DISTINCT route_id) FROM catalog.route_schedule_profile "
            "WHERE runtime_override_min IS NOT NULL"
        ),
    }
    result = {k: _q(cur, sql) for k, sql in queries.items()}
    conn.close()
    return result


@st.cache_data(ttl=60, show_spinner=False)
def fetch_route_gaps() -> list:
    conn = _get_conn()
    rows = _q_all(conn, """
        SELECT r.route_name,
            COALESCE(rs.route_ref, '?') AS route_ref,
            CASE WHEN cs.route_id IS NOT NULL THEN 'Y' ELSE 'N' END AS has_semantics,
            CASE WHEN csp.route_id IS NOT NULL THEN 'Y' ELSE 'N' END AS has_schedule,
            CASE WHEN csd.route_id IS NOT NULL THEN 'Y' ELSE 'N' END AS has_service_days,
            CASE WHEN rro.route_id IS NOT NULL THEN 'Y' ELSE 'N' END AS has_research_runtime
        FROM route_prod.routes r
        LEFT JOIN route_prod.route_semantics rs ON rs.route_id = r.route_id
        LEFT JOIN catalog.route_semantics cs ON cs.route_id = r.route_id
        LEFT JOIN (SELECT DISTINCT route_id FROM catalog.route_schedule_profile) csp
            ON csp.route_id = r.route_id
        LEFT JOIN (SELECT DISTINCT route_id FROM catalog.route_service_days) csd
            ON csd.route_id = r.route_id
        LEFT JOIN (
            SELECT DISTINCT route_id FROM catalog.route_schedule_profile
            WHERE runtime_override_min IS NOT NULL
        ) rro ON rro.route_id = r.route_id
        ORDER BY r.route_name
    """)
    conn.close()
    return rows


# ── Cycle detection ─────────────────────────────────────────

def detect_cycle(h: dict) -> dict:
    total = h["routes"]
    sem = h["semantics"]
    sched = h["schedules"]

    if total == 0:
        return {
            "cycle": 0, "label": "Cycle 0 — Context + OSM",
            "prompt_file": PROMPT_FILES[0],
            "description": "No routes exist. Run 06-zero to establish canton context.",
        }
    if sem == 0 and sched == 0:
        return {
            "cycle": 1, "label": "Cycle 1 — Route Inventory (06a)",
            "prompt_file": PROMPT_FILES[1],
            "description": "Routes exist but no catalogs. Run 06a to inventory routes.",
        }
    if sched < total:
        return {
            "cycle": 2, "label": "Cycle 2 — Multi-batch Cataloging (06b)",
            "prompt_file": f"{PROMPT_FILES['06b-S']} + {PROMPT_FILES['06b-R']}",
            "description": f"{sched}/{total} cataloged. Fill gaps with 06b-S and 06b-R.",
        }
    return {
        "cycle": 3, "label": "Cycle 3 — Fares + Finalize",
        "prompt_file": PROMPT_FILES[3],
        "description": "All routes cataloged. Run 06c for fares or compile GTFS.",
    }


# ── UI helpers ──────────────────────────────────────────────

def _bar(label: str, have: int, total: int):
    pct = (have / total * 100) if total > 0 else 0
    color = "#4caf50" if pct >= 80 else "#ff9800" if pct >= 50 else "#f44336"
    st.markdown(
        f"""
        <div style="margin-bottom:10px;">
          <div style="display:flex; justify-content:space-between; font-size:13px;">
            <span>{label}</span>
            <span><b>{have:,}</b> / {total:,} ({pct:.0f}%)</span>
          </div>
          <div style="background:rgba(255,255,255,0.08); border-radius:6px; height:20px; overflow:hidden;">
            <div style="background:{color}; width:{min(pct,100):.1f}%; height:100%; border-radius:6px; transition:width 0.3s;"></div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _cycle_color(cycle: int) -> str:
    return {0: "#f44336", 1: "#ff9800", 2: "#2196f3", 3: "#4caf50"}.get(cycle, "#999")


# ── Page ────────────────────────────────────────────────────

def main():
    st.markdown(
        """
        <div style="
          border:1px solid rgba(255,255,255,0.12);
          border-radius:12px;
          padding:16px 20px;
          background: linear-gradient(135deg, rgba(10,10,40,0.95) 0%, rgba(20,20,60,0.90) 100%);
          margin-bottom:20px;
        ">
          <div style="font-size:22px; font-weight:700; color:white;">HADES Pipeline Dashboard</div>
          <div style="opacity:0.7; margin-top:4px; font-size:13px; color:white;">
            Cycle status, catalog coverage, gaps (06b-S + 06b-R), GTFS readiness
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    col_r, _ = st.columns([0.12, 0.88])
    with col_r:
        if st.button("Refresh", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    with st.spinner("Querying pipeline state..."):
        h = fetch_health()
        all_routes = fetch_route_gaps()

    total = h["routes"]
    c_info = detect_cycle(h)

    # ── Cycle status banner ──────────────────────
    cyc_color = _cycle_color(c_info["cycle"])
    st.markdown(
        f"""
        <div style="
          border-left: 5px solid {cyc_color};
          padding: 12px 16px;
          margin-bottom: 16px;
          background: rgba(255,255,255,0.04);
          border-radius: 0 8px 8px 0;
        ">
          <div style="font-size:16px; font-weight:700; color:white;">{c_info['label']}</div>
          <div style="opacity:0.8; margin-top:4px; font-size:13px; color:white;">{c_info['description']}</div>
          <div style="margin-top:6px; font-size:13px;">
            <span style="opacity:0.6;">Prompt file:</span>
            <code style="background:rgba(255,255,255,0.1); padding:2px 8px; border-radius:4px;">{c_info['prompt_file']}</code>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── KPI row ──────────────────────────────────
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Nodes", f"{h['nodes']:,}")
    c2.metric("Places", f"{h['places']:,}")
    c3.metric("Routes", f"{total:,}")
    c4.metric("Schedules", f"{h['schedules']:,}")
    c5.metric("Estimates", f"{h['estimates']:,}")
    c6.metric("GTFS Feeds", f"{h['gtfs_feeds']:,}")

    st.divider()

    # ── Phase coverage bars ──────────────────────
    st.markdown("### Phase Coverage")
    col1, col2 = st.columns(2)

    with col1:
        _bar("Place embeddings", h["embeddings"], h["places"])
        _bar("Route semantics", h["semantics"], total)
        _bar("Schedule profiles", h["schedules"], total)

    with col2:
        _bar("Service days", h["service_days"], total)
        _bar("Research runtimes", h.get("research_runtimes", 0), h["schedules"] if h["schedules"] > 0 else total)
        _bar("GTFS threshold (80%)", h["schedules"], int(total * 0.80) if total > 0 else 1)

    st.divider()

    # ── GTFS readiness + Warnings side by side ───
    left, right = st.columns(2)

    with left:
        st.markdown("### GTFS Readiness")
        if total == 0:
            st.info("No routes")
        else:
            sched = h["schedules"]
            cov = sched / total

            st.metric("Catalog coverage", f"{cov * 100:.0f}%",
                       delta="READY" if cov >= 0.80 else f"need {int(total * 0.80) - sched} more")

            if cov >= 1.0:
                st.success("100% — compile full GTFS")
            elif cov >= 0.80:
                st.success(f"{cov*100:.0f}% — ready to compile ({total - sched} excluded)")
            elif cov >= 0.50:
                needed = int(total * 0.80) - sched
                n_s = (needed + BATCH_06B_S - 1) // BATCH_06B_S
                st.warning(f"Need {needed} more routes (~{n_s} 06b-S prompts of ~{BATCH_06B_S})")
            else:
                needed = int(total * 0.80) - sched
                st.error(f"Need {needed} more routes to reach 80%")

    with right:
        st.markdown("### Warnings")
        warnings = []
        if h["garbage_names"] > 0:
            warnings.append(("HIGH", f"{h['garbage_names']} garbage place names"))
        if h["places"] > 0 and h["embeddings"] < h["places"]:
            warnings.append(("HIGH", f"{h['places'] - h['embeddings']} places missing embeddings"))
        if h["semantics"] < total:
            warnings.append(("MED", f"{total - h['semantics']} routes missing semantics"))
        if h["schedules"] < total:
            warnings.append(("HIGH", f"{total - h['schedules']} routes missing schedules — need 06b-S"))
        if h["service_days"] < total:
            warnings.append(("MED", f"{total - h['service_days']} routes missing service days"))
        research_rt = h.get("research_runtimes", 0)
        if h["schedules"] > research_rt:
            warnings.append(("HIGH", f"{h['schedules'] - research_rt} routes have schedules but no research runtimes — need 06b-R"))

        if not warnings:
            st.success("All phases up to date")
        else:
            for pri, msg in warnings:
                if pri == "HIGH":
                    st.error(f"**[{pri}]** {msg}")
                else:
                    st.warning(f"**[{pri}]** {msg}")

    st.divider()

    # ── Route gap tables (06b-S + 06b-R) ─────────
    gaps_s = [r for r in all_routes if r.get("has_schedule") == "N"]
    gaps_r = [r for r in all_routes if r.get("has_schedule") == "Y" and r.get("has_research_runtime") == "N"]
    complete = [r for r in all_routes if r.get("has_schedule") == "Y" and r.get("has_research_runtime") == "Y"]

    display_cols = ["route_ref", "route_name", "has_semantics", "has_schedule", "has_service_days", "has_research_runtime"]

    # 06b-S
    st.markdown("### 06b-S Schedule Gaps")
    if not gaps_s:
        st.success("All routes have schedule profiles")
    else:
        n_batches_s = (len(gaps_s) + BATCH_06B_S - 1) // BATCH_06B_S
        st.error(f"**{len(gaps_s)}** routes missing schedules — **{n_batches_s}** prompts of ~{BATCH_06B_S} — `{PROMPT_FILES['06b-S']}`")

        tab_labels = [f"06b-S #{i+1} ({len(gaps_s[i*BATCH_06B_S:(i+1)*BATCH_06B_S])})" for i in range(n_batches_s)]
        tabs = st.tabs(tab_labels)
        for i, tab in enumerate(tabs):
            batch = gaps_s[i * BATCH_06B_S: (i + 1) * BATCH_06B_S]
            with tab:
                df = pd.DataFrame(batch)
                cols = [c for c in display_cols if c in df.columns]
                st.dataframe(df[cols], use_container_width=True, hide_index=True)

    # 06b-R
    st.markdown("### 06b-R Runtime Gaps")
    if not gaps_r:
        st.success("All cataloged routes have runtime estimates")
    else:
        n_batches_r = (len(gaps_r) + BATCH_06B_R - 1) // BATCH_06B_R
        st.warning(f"**{len(gaps_r)}** routes have schedules but no estimate — **{n_batches_r}** prompts of ~{BATCH_06B_R} — `{PROMPT_FILES['06b-R']}`")

        tab_labels = [f"06b-R #{i+1} ({len(gaps_r[i*BATCH_06B_R:(i+1)*BATCH_06B_R])})" for i in range(n_batches_r)]
        tabs = st.tabs(tab_labels)
        for i, tab in enumerate(tabs):
            batch = gaps_r[i * BATCH_06B_R: (i + 1) * BATCH_06B_R]
            with tab:
                df = pd.DataFrame(batch)
                cols = [c for c in display_cols if c in df.columns]
                st.dataframe(df[cols], use_container_width=True, hide_index=True)

    # Complete
    if complete:
        with st.expander(f"Complete routes ({len(complete)})"):
            df = pd.DataFrame(complete)
            cols = [c for c in display_cols if c in df.columns]
            st.dataframe(df[cols], use_container_width=True, hide_index=True)

    st.divider()

    # ── Next actions ─────────────────────────────
    st.markdown("### Next Actions")
    cycle = c_info["cycle"]
    sched = h["schedules"]
    research_rt = h.get("research_runtimes", 0)

    actions = []
    if h["garbage_names"] > 0:
        actions.append(("HIGH", "Fix garbage names", "`python -m phase2_semantics.scripts.26b_fast_contextual_names --apply`"))

    if cycle == 0:
        actions.append(("HIGH", "Run 06-zero for canton context", f"Open `{PROMPT_FILES[0]}`"))
    elif cycle == 1:
        actions.append(("HIGH", "Run 06a route inventory", f"Open `{PROMPT_FILES[1]}`"))
    elif cycle == 2:
        gap_s = total - sched
        gap_r = sched - research_rt if sched > research_rt else 0
        if gap_s > 0:
            n_s = (gap_s + BATCH_06B_S - 1) // BATCH_06B_S
            actions.append(("HIGH", f"Fill {gap_s}-route schedule gap", f"{n_s} x `{PROMPT_FILES['06b-S']}` (~{BATCH_06B_S} routes each)"))
        if gap_r > 0:
            n_r = (gap_r + BATCH_06B_R - 1) // BATCH_06B_R
            actions.append(("HIGH", f"Fill {gap_r}-route runtime gap", f"{n_r} x `{PROMPT_FILES['06b-R']}` (~{BATCH_06B_R} routes each)"))
    elif cycle == 3:
        actions.append(("MED", "Run 06c for fares", f"Open `{PROMPT_FILES[3]}`"))

    if total > 0 and sched / total >= 0.80:
        actions.append(("MED", "Compile GTFS", f"`python build_canton_gtfs.py --use-v2` ({sched} routes ready)"))

    actions.append(("LOW", "Start next canton", f"Open `{PROMPT_FILES[0]}`"))

    for pri, action, how in actions:
        if pri == "HIGH":
            st.error(f"**{action}** — {how}")
        elif pri == "MED":
            st.warning(f"**{action}** — {how}")
        else:
            st.info(f"**{action}** — {how}")


if __name__ == "__main__":
    main()
