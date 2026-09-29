#!/usr/bin/env python3
"""
Rich terminal UI for the HADES Work Queue Dashboard.

Usage:
    python -m phase5_gtfs.scripts.work_queue_dashboard_rich
    python -m phase5_gtfs.scripts.work_queue_dashboard_rich --gaps
    python -m phase5_gtfs.scripts.work_queue_dashboard_rich --next-action
"""
from __future__ import annotations

import argparse
import os
import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.columns import Columns
from rich import box

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

# Batch sizes per prompt type
BATCH_06B_S = 18   # 15-20 routes per schedule prompt
BATCH_06B_R = 9    # 8-10 routes per runtime prompt

console = Console()


def get_conn():
    import psycopg2
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = True
    return conn


def _q(cur, sql: str) -> int:
    try:
        cur.execute(sql)
        return cur.fetchone()[0]
    except Exception:
        return 0


# ── Data fetching ───────────────────────────────────────────

def fetch_health(conn) -> dict:
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
    return {k: _q(cur, sql) for k, sql in queries.items()}


def fetch_gaps(conn) -> list:
    import psycopg2.extras
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
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
        return cur.fetchall()
    except Exception as e:
        console.print(f"[red]ERROR: {e}[/red]")
        return []


# ── Cycle detection ─────────────────────────────────────────

def detect_cycle(h: dict) -> dict:
    total = h["routes"]
    sem = h["semantics"]
    sched = h["schedules"]

    if total == 0:
        return {
            "cycle": 0,
            "label": "Cycle 0 — Context + OSM",
            "prompt_file": PROMPT_FILES[0],
            "description": "No routes exist. Run 06-zero to establish canton context.",
        }
    if sem == 0 and sched == 0:
        return {
            "cycle": 1,
            "label": "Cycle 1 — Route Inventory (06a)",
            "prompt_file": PROMPT_FILES[1],
            "description": "Routes exist but no catalogs. Run 06a to inventory routes.",
        }
    if sched < total:
        return {
            "cycle": 2,
            "label": "Cycle 2 — Multi-batch Cataloging (06b)",
            "prompt_file": f"{PROMPT_FILES['06b-S']} + {PROMPT_FILES['06b-R']}",
            "description": f"{sched}/{total} cataloged. Fill gaps with 06b-S and 06b-R.",
        }
    return {
        "cycle": 3,
        "label": "Cycle 3 — Fares + Finalize",
        "prompt_file": PROMPT_FILES[3],
        "description": "All routes cataloged. Run 06c for fares or compile GTFS.",
    }


# ── Render sections ─────────────────────────────────────────

def _pct_color(pct: float) -> str:
    if pct >= 80:
        return "green"
    if pct >= 50:
        return "yellow"
    return "red"


def _yn(val):
    return "[green]Y[/]" if val == "Y" else "[red]N[/]"


def render_cycle(c_info: dict):
    cycle = c_info["cycle"]
    color = {0: "red", 1: "yellow", 2: "bright_blue", 3: "green"}.get(cycle, "white")

    lines = [
        f"[bold]{c_info['label']}[/]",
        "",
        c_info["description"],
        "",
        f"Prompt: [bold cyan]{c_info['prompt_file']}[/]",
    ]
    console.print(Panel("\n".join(lines), title="Cycle Status", border_style=color))


def render_health(h: dict):
    total = h["routes"]

    kpi_panels = [
        Panel(f"[bold cyan]{h['nodes']:,}[/]", title="Nodes", border_style="cyan", width=16),
        Panel(f"[bold cyan]{h['places']:,}[/]", title="Places", border_style="cyan", width=16),
        Panel(f"[bold cyan]{total:,}[/]", title="Routes", border_style="cyan", width=16),
        Panel(f"[bold cyan]{h['gtfs_feeds']:,}[/]", title="GTFS Feeds", border_style="cyan", width=16),
    ]
    console.print(Columns(kpi_panels, padding=(0, 1)))
    console.print()

    bars = [
        ("Semantics", h["semantics"], total),
        ("Schedules", h["schedules"], total),
        ("Service Days", h["service_days"], total),
        ("Research RT", h.get("research_runtimes", 0), h["schedules"] if h["schedules"] > 0 else total),
        ("Estimates", h["estimates"], total),
        ("Embeddings", h["embeddings"], h["places"]),
    ]

    table = Table(title="Phase Coverage", box=box.ROUNDED, show_lines=False)
    table.add_column("Component", style="bold", width=16)
    table.add_column("Have", justify="right", width=8)
    table.add_column("Total", justify="right", width=8)
    table.add_column("Coverage", justify="right", width=10)
    table.add_column("Bar", width=30)

    for label, have, of in bars:
        pct = (have / of * 100) if of > 0 else 0
        color = _pct_color(pct)
        filled = int(pct / 100 * 20)
        bar = f"[{color}]{'█' * filled}[/]{'░' * (20 - filled)}"
        table.add_row(label, str(have), str(of), f"[{color}]{pct:.0f}%[/]", bar)

    console.print(table)


def render_warnings(h: dict):
    total = h["routes"]
    warnings = []

    if h["garbage_names"] > 0:
        warnings.append(("HIGH", f"{h['garbage_names']} garbage place names"))
    if h["places"] > 0 and h["embeddings"] < h["places"]:
        warnings.append(("HIGH", f"{h['places'] - h['embeddings']} places missing embeddings"))
    if h["semantics"] < total:
        warnings.append(("MEDIUM", f"{total - h['semantics']} routes missing semantics"))
    if h["schedules"] < total:
        warnings.append(("HIGH", f"{total - h['schedules']} routes missing schedules — need 06b-S"))
    if h["service_days"] < total:
        warnings.append(("MEDIUM", f"{total - h['service_days']} routes missing service days"))
    research_rt = h.get("research_runtimes", 0)
    if h["schedules"] > research_rt:
        warnings.append(("HIGH", f"{h['schedules'] - research_rt} routes have schedules but no research runtimes — need 06b-R"))

    if not warnings:
        console.print(Panel("[bold green]All phases up to date[/]", title="Bundles", border_style="green"))
        return

    table = Table(title="Incomplete Bundles", box=box.ROUNDED)
    table.add_column("Priority", width=10)
    table.add_column("Issue")

    for pri, msg in warnings:
        color = "red" if pri == "HIGH" else "yellow"
        table.add_row(f"[{color}]{pri}[/]", msg)

    console.print(table)


def render_gtfs_readiness(h: dict):
    total = h["routes"]
    if total == 0:
        console.print("[dim]No routes — nothing to compile[/dim]")
        return

    sched = h["schedules"]
    cov = sched / total

    if cov >= 1.0:
        status = "[bold green]READY — 100% coverage[/]"
    elif cov >= 0.80:
        status = f"[bold green]READY — {cov*100:.0f}% ({sched} routes)[/]"
    elif cov >= 0.50:
        needed = int(total * 0.80) - sched
        status = f"[bold yellow]APPROACHING — need {needed} more[/]"
    else:
        needed = int(total * 0.80) - sched
        status = f"[bold red]NOT READY — need {needed} more[/]"

    panel_text = (
        f"Cataloged: [bold]{sched}[/] / {total}  ({cov*100:.0f}%)\n"
        f"Estimated: [bold]{h['estimates']}[/] / {total}\n"
        f"Threshold: 80%\n"
        f"Status: {status}"
    )
    console.print(Panel(panel_text, title="GTFS Readiness", border_style="blue"))


def _render_batch_table(title: str, batch: list, show_runtime: bool = False):
    table = Table(title=title, box=box.SIMPLE_HEAVY, show_lines=False)
    table.add_column("Ref", style="bold", width=10)
    table.add_column("Name", width=45)
    table.add_column("Sem", justify="center", width=5)
    table.add_column("Sched", justify="center", width=5)
    table.add_column("Days", justify="center", width=5)
    if show_runtime:
        table.add_column("RT", justify="center", width=5)

    for r in batch:
        name = (r.get("route_name") or "unnamed")[:44]
        ref = r.get("route_ref", "?")
        row = [
            ref, name,
            _yn(r.get("has_semantics")),
            _yn(r.get("has_schedule")),
            _yn(r.get("has_service_days")),
        ]
        if show_runtime:
            row.append(_yn(r.get("has_research_runtime")))
        table.add_row(*row)

    console.print(table)
    console.print()


def render_gaps(gaps: list):
    # Split into 06b-S (missing schedule) and 06b-R (has schedule but no research runtime)
    gaps_s = [r for r in gaps if r.get("has_schedule") == "N"]
    gaps_r = [r for r in gaps if r.get("has_schedule") == "Y" and r.get("has_research_runtime") == "N"]

    if not gaps_s and not gaps_r:
        console.print(Panel("[bold green]All routes have schedules and estimates[/]", title="Gaps", border_style="green"))
        return

    # ── 06b-S: Schedule gaps ──
    if gaps_s:
        n_batches = (len(gaps_s) + BATCH_06B_S - 1) // BATCH_06B_S
        console.print(Panel(
            f"[bold red]{len(gaps_s)}[/] routes missing schedules -> [bold]{n_batches}[/] prompts of ~{BATCH_06B_S}\n"
            f"Prompt file: [bold cyan]{PROMPT_FILES['06b-S']}[/]",
            title="06b-S SCHEDULE GAPS",
            border_style="red",
        ))

        for i in range(n_batches):
            batch = gaps_s[i * BATCH_06B_S: (i + 1) * BATCH_06B_S]
            _render_batch_table(f"06b-S Prompt {i+1} ({len(batch)} routes)", batch, show_runtime=True)
    else:
        console.print(Panel("[bold green]All routes have schedule profiles[/]", title="06b-S", border_style="green"))

    # ── 06b-R: Runtime gaps ──
    if gaps_r:
        n_batches = (len(gaps_r) + BATCH_06B_R - 1) // BATCH_06B_R
        console.print(Panel(
            f"[bold yellow]{len(gaps_r)}[/] routes have schedules but no estimate -> [bold]{n_batches}[/] prompts of ~{BATCH_06B_R}\n"
            f"Prompt file: [bold cyan]{PROMPT_FILES['06b-R']}[/]",
            title="06b-R RUNTIME GAPS",
            border_style="yellow",
        ))

        for i in range(n_batches):
            batch = gaps_r[i * BATCH_06B_R: (i + 1) * BATCH_06B_R]
            _render_batch_table(f"06b-R Prompt {i+1} ({len(batch)} routes)", batch, show_runtime=True)
    else:
        console.print(Panel("[bold green]All cataloged routes have runtime estimates[/]", title="06b-R", border_style="green"))

    # Summary
    n_s = (len(gaps_s) + BATCH_06B_S - 1) // BATCH_06B_S if gaps_s else 0
    n_r = (len(gaps_r) + BATCH_06B_R - 1) // BATCH_06B_R if gaps_r else 0
    console.print(
        f"  [bold]TOTAL:[/] {len(gaps_s)} schedule gaps ([bold]{n_s}[/] 06b-S prompts) "
        f"+ {len(gaps_r)} runtime gaps ([bold]{n_r}[/] 06b-R prompts)\n"
    )


def render_next_actions(h: dict, c_info: dict):
    total = h["routes"]
    sched = h["schedules"]
    research_rt = h.get("research_runtimes", 0)
    cycle = c_info["cycle"]

    actions = []

    if h["garbage_names"] > 0:
        actions.append(("HIGH", "Fix garbage names", "python -m phase2_semantics.scripts.26b_fast_contextual_names --apply"))

    if cycle == 0:
        actions.append(("HIGH", "Run 06-zero for canton context", f"Open {PROMPT_FILES[0]}"))
    elif cycle == 1:
        actions.append(("HIGH", "Run 06a route inventory", f"Open {PROMPT_FILES[1]}"))
    elif cycle == 2:
        gap_s = total - sched
        gap_r = sched - research_rt if sched > research_rt else 0
        if gap_s > 0:
            n_s = (gap_s + BATCH_06B_S - 1) // BATCH_06B_S
            actions.append(("HIGH", f"Fill {gap_s}-route schedule gap", f"{n_s} x {PROMPT_FILES['06b-S']} (~{BATCH_06B_S} routes each)"))
        if gap_r > 0:
            n_r = (gap_r + BATCH_06B_R - 1) // BATCH_06B_R
            actions.append(("HIGH", f"Fill {gap_r}-route runtime gap", f"{n_r} x {PROMPT_FILES['06b-R']} (~{BATCH_06B_R} routes each)"))
    elif cycle == 3:
        actions.append(("MEDIUM", "Run 06c for fares", f"Open {PROMPT_FILES[3]}"))

    if total > 0 and sched / total >= 0.80:
        actions.append(("MEDIUM", "Compile GTFS", f"build_canton_gtfs.py --use-v2 ({sched} routes)"))

    actions.append(("LOW", "Start next canton", f"Open {PROMPT_FILES[0]}"))

    table = Table(title="Next Actions", box=box.ROUNDED)
    table.add_column("Priority", width=10)
    table.add_column("Action", width=34)
    table.add_column("How")

    for pri, action, how in actions:
        color = {"HIGH": "red", "MEDIUM": "yellow", "LOW": "dim"}.get(pri, "white")
        table.add_row(f"[{color}]{pri}[/]", f"[bold]{action}[/]", how)

    console.print(table)


# ── Main ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="HADES Work Queue Dashboard (Rich)")
    parser.add_argument("--gaps", action="store_true", help="Show only route gaps (06b-S + 06b-R)")
    parser.add_argument("--next-action", action="store_true", help="Show only next actions")
    args = parser.parse_args()

    console.print(Panel(
        "[bold white]HADES Pipeline Dashboard[/]",
        border_style="bright_blue",
        padding=(0, 2),
    ))

    conn = get_conn()

    if args.gaps:
        gaps = fetch_gaps(conn)
        render_gaps(gaps)
        conn.close()
        return

    h = fetch_health(conn)
    c_info = detect_cycle(h)

    if args.next_action:
        render_cycle(c_info)
        console.print()
        render_next_actions(h, c_info)
        conn.close()
        return

    render_cycle(c_info)
    console.print()
    render_health(h)
    console.print()
    render_warnings(h)
    console.print()
    render_gtfs_readiness(h)
    console.print()

    gaps = fetch_gaps(conn)
    render_gaps(gaps)

    render_next_actions(h, c_info)

    conn.close()
    console.print(f"\n[dim]Run: python -m phase5_gtfs.scripts.work_queue_dashboard_rich[/dim]")


if __name__ == "__main__":
    main()
