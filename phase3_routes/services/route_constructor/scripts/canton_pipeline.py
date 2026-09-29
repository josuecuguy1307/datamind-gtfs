#!/usr/bin/env python3
"""
Canton Pipeline Orchestrator — processes a canton through all phases in batch order.

Usage:
    python canton_pipeline.py --canton cayambe --cycle 0 --input 06_zero.json
    python canton_pipeline.py --canton cayambe --cycle 1 --input 06a_routes.json
    python canton_pipeline.py --canton cayambe --cycle 2 --input 06b_schedules.json
    python canton_pipeline.py --canton cayambe --cycle 3 --input 06c_fares.json
    python canton_pipeline.py --canton cayambe --cycle 3 --use-default-fares

Cycles:
    0 = Context + automatic OSM build (06-zero input)
    1 = Gap fill from Deep Research (06a input)
    2 = Schedules + GTFS (06b input)
    3 = Fares + finalize (06c input or --use-default-fares)

Batch Discipline:
    Each stage must COMPLETE for ALL items before the next stage starts.
    No interleaving: do NOT process node A while extracting node B.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
load_dotenv()

# Ensure src is importable
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.db.conn import db_conn, db_cursor
from src.db.route_raw_repo import create_route_job
from src.evidence.overpass import search_route_relations, fetch_relation_overpass_json
from src.evidence.parse_relation import extract_stop_prior
from src.settings import OVERPASS_URL

# Phase 3 stop-grounding + discovery pipeline (Skill 03 §Operation 1).
# These live in datamind_console/; the ML DATAMIND project root must be on
# PYTHONPATH when canton_pipeline is executed (it is, via the CLI wrapper).
from datamind_console.phases.phase3_routes.stop_grounding.discovery_pipeline import (
    run_discovery_pipeline,
)
from datamind_console.phases.phase3_routes.stop_grounding.dual_catalog_loader import (
    AreaDefinition,
    DualCatalogContext,
    RouteGeography,
)
from datamind_console.phases.phase3_routes.stop_grounding.typed_token_dispatch import (
    intake_typed_seed,
    typed_seed_to_route_seed,
)
from datamind_console.phases.phase3_routes.stop_grounding.batch_discovery import (
    _UPSERT_SQL as _DISCOVERY_RUNS_UPSERT_SQL,
    _summary_to_db_params as _discovery_summary_to_db_params,
)
from datamind_console.phases.phase3_routes.stop_grounding.a2_synthesis_bridge import A2RunInputs
from datamind_console.phases.phase3_routes.stop_grounding.termini_utils import extract_termini
from datamind_console.persistence import write_to_route_prod

# HADES Phase 3 enforcers — mandatory gate between quality-gate-pass and
# route_prod write. See workspace/phase3_wiring_log.md §Prompt 7b for the
# full contract.
from hades.enforcers.phase3_coordinator import (
    EnforcerResult,
    RouteContext,
    run_phase3_enforcers,
)

from src.db.route_work_repo import insert_geometry_candidate

_A2_ENABLED = os.environ.get("HADES_DISABLE_A2") != "1"

_PIPELINE_VERSION = "canton_pipeline._promote_summary_to_route_prod"

# Supported policy profiles accepted by the approval_queue CHECK and by
# hades.enforcers.policy_engine.decide. Kept in sync with 031_approval_queue.sql.
_VALID_POLICY_PROFILES = ("conservative", "balanced", "aggressive_supervised")

# ── Directories ──────────────────────────────────────────────

STATE_DIR = os.getenv(
    "CANTON_STATE_DIR",
    os.path.join(Path.home(), "Desktop", "phase3_route_catalog", "cantons"),
)

BBox = Tuple[float, float, float, float]  # (south, west, north, east)


# ═════════════════════════════════════════════════════════════
# Canton Pipeline
# ═════════════════════════════════════════════════════════════

class CantonPipeline:
    """Master orchestrator: drives a canton through all phases in batch order."""

    def __init__(
        self,
        canton: str,
        province: Optional[str] = None,
        *,
        policy_profile: Optional[str] = None,
    ):
        self.canton = canton
        # Province is optional and only used for downstream resolvers
        # (catalog lookups, Nominatim bias, operator keywords). When
        # None, downstream code falls back to the default province
        # defined in ``workspace/config/supported_provinces.json``.
        self.province = (province or "").strip().lower() or None
        # Policy profile for hades.enforcers decision — ctor arg wins,
        # PHASE3_POLICY_PROFILE env falls through, default is "balanced".
        resolved_profile = (
            (policy_profile or os.getenv("PHASE3_POLICY_PROFILE") or "balanced")
            .strip()
            .lower()
        )
        if resolved_profile not in _VALID_POLICY_PROFILES:
            raise ValueError(
                f"Unknown policy_profile={resolved_profile!r}. "
                f"Valid: {_VALID_POLICY_PROFILES}"
            )
        self.policy_profile = resolved_profile
        self.canton_dir = os.path.join(STATE_DIR, canton)
        self.state_file = os.path.join(self.canton_dir, "state.json")
        self.report_lines: List[str] = []

        os.makedirs(os.path.join(self.canton_dir, "inbox"), exist_ok=True)
        os.makedirs(os.path.join(self.canton_dir, "processing"), exist_ok=True)
        os.makedirs(os.path.join(self.canton_dir, "completed"), exist_ok=True)
        os.makedirs(os.path.join(self.canton_dir, "reports"), exist_ok=True)

    # ── State ────────────────────────────────────────────────

    def load_state(self) -> Dict[str, Any]:
        if os.path.exists(self.state_file):
            with open(self.state_file) as f:
                return json.load(f)
        return {
            "canton": self.canton,
            "current_cycle": "not_started",
            "cycles_completed": [],
            # Phase 1-2 (BATCH — extract all, process all)
            "nodes_extracted": 0,
            "parishes_extracted": 0,
            "places_created": 0,
            "phase1_complete": False,
            "phase2_complete": False,
            # Phase 3 (STREAMING — build each batch as seed catalogs arrive)
            "osm_routes_found": 0,
            "routes_from_osm": 0,
            "routes_from_research": 0,
            "routes_built": 0,
            "route_batches_built": 0,
            # Phase 4 (STREAMING — ingest each 06b batch immediately)
            "routes_with_catalog": 0,
            "catalog_batches_received": 0,
            "catalog_coverage_pct": 0.0,
            # Phase 5 (THRESHOLD — compile when >=80% cataloged)
            "gtfs_compiled": False,
            "gtfs_packaged": False,
            "gtfs_routes": 0,
            "gtfs_threshold_met": False,
            # Valhalla traffic (INCREMENTAL)
            "valhalla_edges_loaded": 0,
            "traffic_updates": 0,
            "errors": [],
            "last_updated": datetime.now().isoformat(),
            "next_action": "",
        }

    def save_state(self, state: Dict[str, Any]):
        state["last_updated"] = datetime.now().isoformat()
        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=2)

    def report(self, line: str):
        print(line)
        self.report_lines.append(line)

    def save_report(self, cycle_name: str) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        report_path = os.path.join(
            self.canton_dir, "reports", f"{cycle_name}_{ts}.md"
        )
        with open(report_path, "w") as f:
            f.write(f"# {self.canton.title()} -- {cycle_name}\n\n")
            f.write(f"Generated: {datetime.now().isoformat()}\n\n")
            for line in self.report_lines:
                f.write(line + "\n")
        self.report_lines = []
        return report_path

    # ═════════════════════════════════════════════════════════
    # CYCLE 0: Context + Automatic OSM Build
    # ═════════════════════════════════════════════════════════

    def cycle_0(self, input_file: str):
        """Process 06-zero canton context + automatic OSM build."""
        state = self.load_state()

        with open(input_file) as f:
            research = json.load(f)

        self.report(f"## Cycle 0: Context + Automatic Build for {self.canton.title()}")

        # ── STAGE 1: Infrastructure setup ──
        self.report("\n### Stage 1: Infrastructure Setup")
        self._setup_infrastructure(research)

        # ── STAGE 2: BATCH — Extract ALL nodes (Phase 1) ──
        # Batch discipline: extract ALL parishes before Phase 2 starts.
        # Naming quality depends on ALL nodes being visible.
        self.report("\n### Stage 2: BATCH Extract All Parishes")
        node_count = self._extract_all_nodes(research)
        state["nodes_extracted"] = node_count
        state["phase1_complete"] = True
        self.report(f"  BATCH COMPLETE: {node_count} total nodes")

        # ── STAGE 3: BATCH — Process ALL nodes through Phase 2 ──
        # Batch discipline: process ALL extracted nodes at once.
        # Contextual naming needs full neighborhood context.
        self.report("\n### Stage 3: BATCH Process All Nodes -> Places")
        place_count = self._process_all_nodes()
        state["places_created"] = place_count
        state["phase2_complete"] = True
        self.report(f"  BATCH COMPLETE: {place_count} places created")

        # ── STAGE 4: STREAMING — Discover + Build OSM routes (Phase 3) ──
        # Streaming: route construction is per-route independent.
        self.report("\n### Stage 4: STREAM — Discover + Build OSM Routes")
        osm_count, built_count, failed = self._build_all_osm_routes(research)
        state["osm_routes_found"] = osm_count
        state["routes_from_osm"] = built_count
        state["routes_built"] = built_count
        state["route_batches_built"] = 1
        self.report(f"  OSM relations found: {osm_count}")
        self.report(f"  Routes built: {built_count}")
        if failed:
            state["errors"].extend(
                [f"route_build:{r['name']}:{r['error']}" for r in failed]
            )
            self.report(f"  Routes failed: {len(failed)}")
            for r in failed:
                self.report(f"    - {r['name']}: {r['error']}")

        # ── STAGE 5: STREAMING — Bridge OSM tags -> catalog immediately ──
        self.report("\n### Stage 5: STREAM — Bridge OSM Tags -> Catalog")
        bridged = self._bridge_osm_to_catalog()
        self.report(f"  Routes with OSM-derived catalog data: {bridged}")

        # ── STAGE 6: STREAMING — Name routes immediately ──
        self.report("\n### Stage 6: STREAM — Name Routes")
        named = self._name_all_routes()
        self.report(f"  Routes named: {named}")

        # ── STREAMING: Estimate OSM routes via RuntimeLabV2 ──
        # OSM routes have geometry + names but no Deep Research data yet.
        # Estimates use Valhalla + defaults; will improve when 06b arrives.
        self.report(f"\n### Stage 7: STREAM — Estimate {built_count} OSM Routes")
        estimated = self._estimate_osm_routes()
        self.report(
            f"  Estimated: {estimated} routes "
            f"(Valhalla + defaults, will improve with 06b)"
        )

        # ── Gap analysis ──
        self.report("\n### Gap Analysis")
        gaps = self._identify_gaps(research)

        # Generate OSM route list for pasting into 06a prompt
        osm_route_list = self._generate_osm_route_list_for_06a()

        if gaps["missing_routes"]:
            self.report(
                f"\nRoutes NOT in OSM ({len(gaps['missing_routes'])} estimated):"
            )
            for coop in gaps.get("missing_coops", []):
                self.report(
                    f"  - {coop['name']}: ~{coop['estimated_missing']} routes"
                )
            state["current_cycle"] = "waiting_for_discovery"
            state["next_action"] = (
                "Drop 06a route inventory. "
                "Paste the OSM route list below into the 06a prompt."
            )
        else:
            self.report("\nAll known routes found in OSM. Skipping to schedules.")
            state["current_cycle"] = "waiting_for_schedules"
            state["next_action"] = self._generate_schedule_request()

        # Store OSM route list for 06a prompt
        osm_list_file = os.path.join(
            self.canton_dir, "osm_route_list_for_06a.txt"
        )
        with open(osm_list_file, "w") as f:
            f.write(osm_route_list)

        state["cycles_completed"].append("cycle_0")
        self.save_state(state)
        self.save_report("cycle_0_context")
        self._print_action_box_cycle_0(state, osm_route_list, built_count)
        return state

    # ═════════════════════════════════════════════════════════
    # CYCLE 1: Gap Fill from Deep Research
    # ═════════════════════════════════════════════════════════

    def cycle_1(self, input_file: str):
        """
        Process unified 06a: metadata for ALL routes + construction for non-OSM.

        The 06a JSON contains ALL routes with ``in_osm: true/false``.
        - OSM routes (in_osm=true): enrich metadata only (cooperative, jurisdiction)
        - Non-OSM routes (in_osm=false + seed_catalog): dedup then construct

        STREAMING discipline: each batch is built, bridged, named immediately.
        """
        state = self.load_state()

        with open(input_file) as f:
            research = json.load(f)

        all_routes = research.get("routes", [])
        osm_routes = [r for r in all_routes if r.get("in_osm", False)]
        new_routes = [
            r for r in all_routes if not r.get("in_osm", False)
        ]

        self.report(
            f"## Cycle 1: Unified 06a for {self.canton.title()}\n"
            f"  Total routes: {len(all_routes)} "
            f"({len(osm_routes)} OSM, {len(new_routes)} need construction)"
        )

        # ── FOR ALL ROUTES: enrich metadata ──
        # Even OSM routes get cooperative, jurisdiction, route_type updated.
        # Uses confidence 0.6 (higher than OSM bridge 0.3-0.5, lower than 06b 0.7+).
        self.report(
            f"\n### Enriching ALL {len(all_routes)} routes with research metadata"
        )
        enriched = 0
        for route in all_routes:
            try:
                self._enrich_route_metadata(route)
                enriched += 1
            except Exception as exc:
                code = route.get("route_code", "?")
                self.report(f"  WARN: Could not enrich {code}: {exc}")
        self.report(
            f"  Enriched: {enriched} routes "
            f"(cooperative, jurisdiction, route_type)"
        )

        # ── FOR NON-OSM ROUTES: dedup + construct ──
        built = 0
        expected_builds = 0  # new_routes that have a seed_catalog and are genuinely new
        failed_names: List[str] = []

        if new_routes:
            self.report(
                f"\n### STREAM — Constructing {len(new_routes)} routes NOT in OSM"
            )

            # Build DualCatalogContext once from the 06a file. Downstream
            # helpers (intake_typed_seed, run_discovery_pipeline, the
            # discovery_runs upsert) rely on catalog_ctx.province for
            # Camino D + Skill 11 §7 province threading.
            catalog_ctx = self._build_catalog_ctx_from_research(research)
            self.report(
                f"  DualCatalogContext: province={catalog_ctx.province or 'sample_region'}, "
                f"unit={catalog_ctx.unit_name}, "
                f"{len(catalog_ctx.route_geographies)} route geographies, "
                f"{len(catalog_ctx.area_definitions)} area definitions"
            )

            # Dedup against ALL existing routes
            from src.dedup.route_matcher import (
                match_research_against_existing,
                enrich_existing_route,
            )

            with db_conn() as conn:
                dedup_result = match_research_against_existing(
                    new_routes,
                    self.canton,
                    conn,
                )

                self.report(
                    f"  Dedup: {len(dedup_result['matched'])} already exist "
                    f"(enrich only), {len(dedup_result['new'])} genuinely new, "
                    f"{len(dedup_result['ambiguous'])} ambiguous"
                )

                # Enrich matched
                for match in dedup_result["matched"]:
                    enrich_existing_route(
                        match["existing_route_id"],
                        match["research_route"],
                        conn,
                    )

                # Build genuinely new routes from seed catalogs. The same
                # conn is threaded into _build_from_research so each route's
                # route_job + route_work + route_prod writes land in one
                # transaction and roll back together on failure.
                for new_route in dedup_result["new"]:
                    rr = new_route["research_route"]
                    code = rr.get("route_code", "?")

                    if not rr.get("seed_catalog"):
                        self.report(
                            f"  SKIP: {code} — in_osm=false but no seed_catalog"
                        )
                        continue

                    expected_builds += 1
                    try:
                        success = self._build_from_research(
                            rr,
                            catalog_ctx=catalog_ctx,
                            conn=conn,
                        )
                        if success:
                            built += 1
                        else:
                            failed_names.append(code)
                    except Exception as exc:
                        conn.rollback()
                        failed_names.append(code)
                        self.report(f"  FAILED: {code} -- {exc}")
        else:
            self.report(
                "\n  All routes found in OSM — no construction needed"
            )

        state["routes_from_research"] = (
            state.get("routes_from_research", 0) + built
        )
        state["routes_built"] = state.get("routes_built", 0) + built
        state["route_batches_built"] = (
            state.get("route_batches_built", 0) + 1
        )
        self.report(f"\n  New routes built: {built}")
        self.report(f"  Total routes: {state['routes_built']}")
        if failed_names:
            self.report(f"  Failed: {len(failed_names)} ({', '.join(failed_names)})")

        # ── STREAMING: Bridge + name immediately for this batch ──
        self.report("\n### STREAM — Bridge + Name")
        self._bridge_osm_to_catalog()
        self._name_all_routes()

        # ── STREAMING: Estimate newly built routes ──
        if built > 0:
            self.report(
                f"\n### STREAM — Estimating {built} newly built routes"
            )
            est = self._estimate_osm_routes()
            self.report(f"  Estimated: {est} routes")

        # ── Honesty gate: do NOT mark cycle_1 completed if the build stubs
        # silently returned False for every expected build. Previously this
        # branch unconditionally advanced state to waiting_for_schedules and
        # appended cycle_1 to cycles_completed, producing a misleading status
        # (duran 2026-04-09 false-positive). See open_followups in state.json.
        stub_blocked = expected_builds > 0 and built == 0
        if stub_blocked:
            state["current_cycle"] = "cycle_1_blocked_build_stubs"
            state["next_action"] = (
                f"cycle_1 attempted {expected_builds} seed-catalog builds but "
                f"_build_from_research returned False for all of them "
                f"(it is a stub). Implement the research→constructor bridge, "
                f"or use an alternate entry point "
                f"(construct_canton_routes.py / construct_tuned.py / "
                f"atomic 30→35→40 scripts) and re-run cycle_1."
            )
            state.setdefault("errors", []).append({
                "cycle": "cycle_1",
                "kind": "stub_blocked",
                "expected_builds": expected_builds,
                "built": 0,
                "failed_names": failed_names,
            })
            self.report(
                f"\n  ⚠ cycle_1 NOT marked completed: "
                f"{expected_builds} expected builds, 0 succeeded "
                f"(_build_from_research is a stub)."
            )
            self.save_state(state)
            self.save_report("cycle_1_inventory")
            self._print_action_box(state)
            return state

        # ── Generate schedule request with context ──
        state["current_cycle"] = "waiting_for_schedules"
        state["next_action"] = self._generate_schedule_request_with_context()
        state.setdefault("cycles_completed", []).append("cycle_1")
        self.save_state(state)
        self.save_report("cycle_1_inventory")
        self._print_action_box(state)
        return state

    # ═════════════════════════════════════════════════════════
    # CYCLE 2: Schedules + GTFS
    # ═════════════════════════════════════════════════════════

    def cycle_2(self, input_file: str):
        """
        Process ONE 06b batch — may be called multiple times per canton.

        STREAMING discipline: each 06b batch is ingested independently.
        THRESHOLD: GTFS compiled only when catalog coverage >= 80%.
        INCREMENTAL: Valhalla traffic updated after each batch.
        """
        state = self.load_state()

        with open(input_file) as f:
            research = json.load(f)

        batch_routes = research.get("routes", [])
        batch_size = len(batch_routes)
        batch_num = state.get("catalog_batches_received", 0) + 1

        self.report(
            f"## Cycle 2 — Batch {batch_num}: "
            f"{batch_size} routes for {self.canton.title()}"
        )

        # ── STREAMING: Ingest THIS batch's catalogs only ──
        self.report("\n### STREAM — Ingest Catalog Batch")
        catalog_result = self._ingest_all_catalogs(research)
        batch_ingested = catalog_result.get("total", 0)
        self.report(f"  Semantics: {catalog_result.get('semantics', 0)}")
        self.report(f"  Schedule profiles: {catalog_result.get('schedules', 0)}")
        self.report(f"  Service days: {catalog_result.get('service_days', 0)}")
        self.report(f"  Layover policies: {catalog_result.get('layovers', 0)}")

        # ── STREAMING: Estimate THIS batch's routes via RuntimeLabV2 ──
        self.report("\n### STREAM — Runtime Estimation for Batch")
        runtime_result = self._estimate_all_runtimes()
        self.report(f"  Estimates: {runtime_result.get('estimated', 0)}")
        self.report(f"  Bindings: {runtime_result.get('bound', 0)}")
        self.report(
            f"  Avg confidence: {runtime_result.get('avg_confidence', 'N/A')}"
        )

        # ── INCREMENTAL: Update Valhalla traffic for this batch's edges ──
        self.report("\n### INCREMENTAL — Valhalla Traffic Update")
        traffic_count = self._update_valhalla_traffic(batch_routes)
        state["traffic_updates"] = state.get("traffic_updates", 0) + 1
        state["valhalla_edges_loaded"] = (
            state.get("valhalla_edges_loaded", 0) + traffic_count
        )
        self.report(f"  Edges loaded: {traffic_count}")

        # ��─ Update running totals ──
        state["catalog_batches_received"] = batch_num
        state["routes_with_catalog"] = (
            state.get("routes_with_catalog", 0) + batch_ingested
        )

        # ── Check GTFS coverage threshold ──
        total_routes = state.get("routes_built", 0)
        cataloged = state["routes_with_catalog"]
        coverage = cataloged / total_routes if total_routes > 0 else 0
        state["catalog_coverage_pct"] = round(coverage, 3)

        self.report(
            f"\n### Coverage: {cataloged}/{total_routes} ({coverage * 100:.0f}%)"
        )

        if coverage >= 0.80:
            state["gtfs_threshold_met"] = True

            # ── THRESHOLD MET: Compile GTFS with cataloged routes only ──
            self.report("\n### THRESHOLD MET — Compiling GTFS")
            self.report("  (Including ONLY routes with complete catalogs)")

            vehicle_result = self._compute_all_vehicle_blocks()
            self.report(
                f"  Vehicle profiles updated: {vehicle_result.get('updated', 0)}"
            )

            gtfs_result = self._compile_gtfs()
            state["gtfs_compiled"] = True
            state["gtfs_packaged"] = gtfs_result.get("validation") == "PASS"
            state["gtfs_routes"] = gtfs_result.get("routes", 0)
            self.report(f"  Routes in GTFS: {gtfs_result.get('routes', 0)}")
            self.report(f"  Trips: {gtfs_result.get('trips', 0)}")
            self.report(f"  Stop times: {gtfs_result.get('stop_times', 0)}")
            self.report(
                f"  Validation: {gtfs_result.get('validation', 'UNKNOWN')}"
            )

            if coverage >= 1.0:
                state["current_cycle"] = "waiting_for_fares_or_done"
                state["next_action"] = (
                    "All routes cataloged. Apply fares (--use-default-fares) "
                    "or drop 06c for custom fares."
                )
            else:
                remaining = total_routes - cataloged
                state["current_cycle"] = "waiting_for_more_schedules"
                state["next_action"] = (
                    f"GTFS compiled with {cataloged} routes. "
                    f"{remaining} routes still need 06b. "
                    f"Drop next batch to reach 100%."
                )
        else:
            remaining = total_routes - cataloged
            needed_for_threshold = max(0, int(total_routes * 0.80) - cataloged)
            batches_remaining = (needed_for_threshold + 12) // 13
            state["current_cycle"] = "waiting_for_schedules"
            state["next_action"] = (
                f"{cataloged}/{total_routes} cataloged ({coverage * 100:.0f}%). "
                f"Need {needed_for_threshold} more for 80% threshold. "
                f"~{batches_remaining} more 06b prompts."
            )

        state["cycles_completed"].append(f"cycle_2_batch_{batch_num}")
        self.save_state(state)
        self.save_report(f"cycle_2_batch_{batch_num}")
        self._print_action_box(state)
        return state

    # ═════════════════════════════════════════════════════════
    # CYCLE 3: Fares + Done
    # ═════════════════════════════════════════════════════════

    def cycle_3(self, input_file: Optional[str] = None, use_defaults: bool = False):
        """Finalize with fares."""
        state = self.load_state()

        self.report(f"## Cycle 3: Fares + Finalize for {self.canton.title()}")

        if use_defaults:
            self.report("\n### Applying Default Fares")
            self._apply_default_fares()
        elif input_file:
            self.report("\n### Ingesting Custom Fares")
            self._ingest_fares(input_file)

        # Final GTFS rebuild
        self.report("\n### Final GTFS Rebuild")
        gtfs_result = self._compile_gtfs()
        self.report(f"  Routes: {gtfs_result.get('routes', 0)}")
        self.report(f"  Trips: {gtfs_result.get('trips', 0)}")
        self.report(f"  Validation: {gtfs_result.get('validation', 'UNKNOWN')}")

        state["current_cycle"] = "DONE"
        state["gtfs_packaged"] = True
        state["next_action"] = (
            f"Canton {self.canton} complete. "
            f"{gtfs_result.get('routes', '?')} routes, "
            f"{gtfs_result.get('trips', '?')} trips. Review and publish."
        )
        state["cycles_completed"].append("cycle_3")
        self.save_state(state)
        self.save_report("cycle_3_final")
        self._print_action_box(state)
        return state

    # ═════════════════════════════════════════════════════════
    # Action Box
    # ═════════════════════════════════════════════════════════

    def _print_action_box(self, state: Dict[str, Any]):
        cycle = state.get("current_cycle", "?")
        action = state.get("next_action", "None")
        routes = state.get("routes_built", 0)
        cataloged = state.get("routes_with_catalog", 0)
        coverage_pct = state.get("catalog_coverage_pct", 0)

        print()
        print("=" * 60)
        print(f"  {self.canton.upper()} -- {cycle}")
        print("=" * 60)
        print(f"  Phase 1 complete: {state.get('phase1_complete', False)}")
        print(f"  Phase 2 complete: {state.get('phase2_complete', False)}")
        print(f"  Routes built: {routes}")
        print(
            f"  Catalog coverage: {cataloged}/{routes} "
            f"({coverage_pct * 100:.0f}%)"
        )
        threshold_met = state.get("gtfs_threshold_met", False)
        print(f"  GTFS threshold (80%): {'MET' if threshold_met else 'not met'}")
        print(f"  GTFS compiled: {state.get('gtfs_compiled', False)}")
        print(f"  06b batches received: {state.get('catalog_batches_received', 0)}")
        print(f"  Valhalla traffic updates: {state.get('traffic_updates', 0)}")
        print()
        print(f"  > NEXT ACTION:")
        print(f"  {action}")
        print()
        print(f"  Save results to: {self.canton_dir}/inbox/")
        print("=" * 60)

    # ═════════════════════════════════════════════════════════
    # OSM Route Relation Discovery
    # ═════════════════════════════════════════════════════════

    def _discover_osm_relations(
        self, canton_bbox: BBox
    ) -> List[Dict[str, Any]]:
        """
        Query Overpass for all bus/trolleybus/minibus route relations
        in the canton bbox. Returns list of {id, tags}.
        """
        relations = search_route_relations(
            canton_bbox,
            query_strategy="bbox_first_broad",
            limit=500,
        )
        self.report(f"  Overpass returned {len(relations)} route relations")
        return relations

    def _build_route_via_run_all(
        self,
        osm_relation_id: int,
        *,
        bbox: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Build a single route through the existing run_all.py pipeline.
        Returns {route_id, success, error}.
        """
        scripts_dir = _ROOT / "scripts"
        run_all = scripts_dir / "run_all.py"

        cmd = [sys.executable, str(run_all), "new", str(osm_relation_id)]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(_ROOT)

        result = subprocess.run(
            cmd,
            cwd=str(_ROOT),
            env=env,
            text=True,
            capture_output=True,
            timeout=300,
        )

        if result.returncode != 0:
            error_snippet = (result.stderr or result.stdout or "")[-200:]
            return {
                "osm_relation_id": osm_relation_id,
                "route_id": None,
                "success": False,
                "error": error_snippet.strip(),
            }

        # Try to extract route_id from output
        import re

        route_id = None
        m = re.search(r"route_id:\s*([0-9a-fA-F-]{36})", result.stdout or "")
        if m:
            route_id = m.group(1)

        return {
            "osm_relation_id": osm_relation_id,
            "route_id": route_id,
            "success": True,
            "error": None,
        }

    # ═════════════════════════════════════════════════════════
    # Stage Implementations (Batch Discipline)
    # ═════════════════════════════════════════════════════════

    def _setup_infrastructure(self, research: Dict[str, Any]):
        """
        Register canton in territorial keys, extraction bboxes,
        and geography anchors from research context.
        """
        canton_meta = research.get("canton", {})
        parishes = research.get("parishes", [])

        # Store canton bbox for later stages
        bbox = canton_meta.get("bbox")
        if bbox:
            bbox_file = os.path.join(self.canton_dir, "canton_bbox.json")
            with open(bbox_file, "w") as f:
                json.dump({"canton": self.canton, "bbox": bbox}, f, indent=2)
            self.report(f"  Canton bbox stored: {bbox}")

        # Store parish extraction targets
        targets = []
        for p in parishes:
            if p.get("bbox"):
                targets.append(
                    {
                        "place": p.get("name", "unknown"),
                        "bbox": p["bbox"],
                        "group": p.get("group", self.canton),
                    }
                )
        targets_file = os.path.join(self.canton_dir, "extraction_targets.json")
        with open(targets_file, "w") as f:
            json.dump(targets, f, indent=2)
        self.report(f"  Extraction targets: {len(targets)} parishes")

        # Store cooperatives for gap analysis
        coops = research.get("cooperatives", [])
        if coops:
            coops_file = os.path.join(self.canton_dir, "cooperatives.json")
            with open(coops_file, "w") as f:
                json.dump(coops, f, indent=2)
            self.report(f"  Cooperatives registered: {len(coops)}")

    def _extract_all_nodes(self, research: Dict[str, Any]) -> int:
        """
        BATCH DISCIPLINE:
        1. Collect ALL extraction targets for the canton
        2. Run ALL extractions
        3. Wait for ALL to complete
        4. Count total nodes
        5. ONLY THEN return
        """
        targets_file = os.path.join(self.canton_dir, "extraction_targets.json")
        if not os.path.exists(targets_file):
            self.report("  No extraction targets found. Skipping.")
            return 0

        with open(targets_file) as f:
            targets = json.load(f)

        total_nodes = 0
        for i, target in enumerate(targets, 1):
            place = target.get("place", "unknown")
            bbox = target.get("bbox")
            if not bbox:
                self.report(f"  [{i}/{len(targets)}] SKIP {place}: no bbox")
                continue

            self.report(f"  [{i}/{len(targets)}] Extracting: {place}...")

            # Call batch_extract_fast.py or equivalent
            # For now: count via Overpass query for transit stops
            try:
                count = self._run_node_extraction(bbox, target.get("group", ""))
                total_nodes += count
                self.report(f"    -> {count} nodes")
            except Exception as exc:
                self.report(f"    -> FAILED: {exc}")

        self.report(
            f"  BUNDLE COMPLETE: {total_nodes} total nodes "
            f"across {len(targets)} areas"
        )
        return total_nodes

    def _run_node_extraction(self, bbox: Any, group: str) -> int:
        """
        Run Overpass extraction for a single bbox.
        Returns element count.
        """
        # Delegate to existing batch_extract_fast.py
        scripts_dir = _ROOT / "scripts"
        script = scripts_dir / "batch_extract_fast.py"

        if not script.exists():
            # Fallback: use the Phase 1 extractor directly
            self.report("    (batch_extract_fast.py not found, using stub)")
            return 0

        if isinstance(bbox, list):
            bbox_str = ",".join(str(b) for b in bbox)
        elif isinstance(bbox, str):
            bbox_str = bbox
        else:
            bbox_str = str(bbox)

        cmd = [
            sys.executable,
            str(script),
            "--bbox",
            bbox_str,
            "--group",
            group,
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(_ROOT)

        result = subprocess.run(
            cmd,
            cwd=str(_ROOT),
            env=env,
            text=True,
            capture_output=True,
            timeout=180,
        )

        if result.returncode != 0:
            raise RuntimeError(
                (result.stderr or result.stdout or "unknown error")[-200:]
            )

        # Try to parse count from output
        import re

        m = re.search(r"(\d+)\s*(?:elements|nodes|candidates)", result.stdout or "")
        return int(m.group(1)) if m else 0

    def _process_all_nodes(self) -> int:
        """
        BATCH DISCIPLINE: process ALL extracted nodes through Phase 2.
        normalize -> cluster -> features -> resolve -> promote -> places
        """
        # This delegates to the Phase 1+2 node processing pipeline.
        # Stub: returns 0 until Phase 2 batch processor is wired.
        self.report("  (Phase 2 batch processing: stub -- wire to node pipeline)")
        return 0

    def _build_all_osm_routes(
        self, research: Dict[str, Any]
    ) -> Tuple[int, int, List[Dict[str, str]]]:
        """
        BATCH DISCIPLINE:
        1. Discover ALL OSM route relations in canton bbox
        2. Build ALL routes via Constructor V2
        3. Wait for ALL to complete
        4. Return (found, built, failed)
        """
        canton_bbox = self._get_canton_bbox()
        if not canton_bbox:
            self.report("  No canton bbox found. Skipping OSM discovery.")
            return 0, 0, []

        # Stage A: Discover ALL relations (batch)
        self.report("  [A] Discovering OSM route relations...")
        relations = self._discover_osm_relations(canton_bbox)
        osm_count = len(relations)

        if osm_count == 0:
            self.report("  No OSM route relations found in canton bbox.")
            return 0, 0, []

        # Stage B: Build ALL routes (batch -- one at a time, but all before advancing)
        self.report(f"  [B] Building {osm_count} routes...")
        built = 0
        failed: List[Dict[str, str]] = []

        for i, rel in enumerate(relations, 1):
            rid = rel["id"]
            tags = rel.get("tags", {})
            name = tags.get("name") or tags.get("ref") or f"rel/{rid}"
            self.report(f"    [{i}/{osm_count}] {name} (relation {rid})...")

            try:
                result = self._build_route_via_run_all(rid)
                if result["success"]:
                    built += 1
                    self.report(f"      -> OK (route_id: {result['route_id']})")
                else:
                    failed.append({"name": name, "error": result["error"] or "unknown"})
                    self.report(f"      -> FAILED: {result['error']}")
            except Exception as exc:
                failed.append({"name": name, "error": str(exc)})
                self.report(f"      -> FAILED: {exc}")

        return osm_count, built, failed

    def _bridge_osm_to_catalog(self) -> int:
        """Bridge OSM tags -> catalog.route_semantics for canton routes."""
        # Stub: returns 0 until OSM bridge is implemented
        self.report("  (OSM-to-catalog bridge: stub)")
        return 0

    def _name_all_routes(self) -> int:
        """Name all unnamed routes in the canton."""
        # Stub: returns 0 until naming pipeline is wired
        self.report("  (Route naming: stub)")
        return 0

    def _update_valhalla_traffic(self, batch_routes: List[Dict]) -> int:
        """
        INCREMENTAL: Load Valhalla traffic edges from this batch's routes.
        Each batch improves the traffic model for ALL routes sharing those edges.
        """
        # Stub: returns 0 edges until Valhalla :8004 is running
        # When wired: decompose runtime ranges → edge speeds → load into Valhalla
        self.report(
            "  (Valhalla traffic update: stub — "
            "wire to research_to_valhalla_traffic.py)"
        )
        return 0

    def _estimate_osm_routes(self) -> int:
        """
        Run RuntimeLabV2 on all routes in the canton that don't yet have
        estimates. Uses Valhalla + defaults (no research ranges).
        """
        # Stub: returns 0 until RuntimeLabV2 batch runner is wired
        self.report(
            "  (RuntimeLabV2 estimation: stub — "
            "wire to RuntimeLabV2.estimate_and_bind)"
        )
        return 0

    def _generate_osm_route_list_for_06a(self) -> str:
        """
        Generate the OSM route list to paste into the 06a Deep Research prompt.
        Tells Deep Research which routes are already built.
        """
        canton_bbox = self._get_canton_bbox()
        if not canton_bbox:
            return "  (No canton bbox — list routes manually)"

        lines: List[str] = []
        try:
            s, w, n, e = canton_bbox
            with db_conn() as conn:
                with db_cursor(conn) as cur:
                    cur.execute(
                        """
                        SELECT r.route_name,
                               COALESCE(rs.route_ref, '') AS route_ref,
                               COALESCE(cs.operator, '') AS operator,
                               COALESCE(cs.public_origin, '') AS origin,
                               COALESCE(cs.public_destination, '') AS dest
                        FROM route_prod.routes r
                        LEFT JOIN route_prod.route_semantics rs
                            ON rs.route_id = r.route_id
                        LEFT JOIN catalog.route_semantics cs
                            ON cs.route_id = r.route_id
                        WHERE ST_Intersects(
                            r.geom,
                            ST_MakeEnvelope(%s, %s, %s, %s, 4326)
                        )
                        ORDER BY rs.route_ref, r.route_name
                        """,
                        (w, s, e, n),
                    )
                    for row in cur.fetchall():
                        ref = row["route_ref"] or "?"
                        name = row["route_name"] or "unnamed"
                        op = row["operator"] or "?"
                        orig = row["origin"] or ""
                        dest = row["dest"] or ""
                        td = f" [{orig} -> {dest}]" if orig or dest else ""
                        lines.append(f"  - {ref}: {name} ({op}){td}")
        except Exception:
            return "  (DB query failed — list routes manually)"

        if not lines:
            return (
                "  (No OSM routes found — "
                "all routes need full construction data)"
            )
        return "\n".join(lines)

    def _print_action_box_cycle_0(
        self,
        state: Dict[str, Any],
        osm_route_list: str,
        built_count: int,
    ):
        """Special action box for Cycle 0 that includes the 06a paste block."""
        print()
        print("=" * 60)
        print(f"  {self.canton.upper()} — Cycle 0 Complete")
        print("=" * 60)
        print(f"  Phase 1 complete: {state.get('phase1_complete', False)}")
        print(f"  Phase 2 complete: {state.get('phase2_complete', False)}")
        print(f"  Routes from OSM: {built_count}")
        print()
        print("  PASTE THIS INTO DEEP RESEARCH 06a:")
        print()
        print("  Routes already in OpenStreetMap (have geometry):")
        print(osm_route_list or "  (none)")
        print()
        print(
            "  Deep Research will provide basic info for ALL routes"
        )
        print(
            "  and extra GPS coordinates for routes NOT in this list."
        )
        print()
        print(f"  > NEXT ACTION:")
        print(f"  {state.get('next_action', 'None')}")
        print()
        osm_list_file = os.path.join(
            self.canton_dir, "osm_route_list_for_06a.txt"
        )
        print(f"  OSM list saved to: {osm_list_file}")
        print(f"  Save 06a JSON to: {self.canton_dir}/inbox/")
        print("=" * 60)

    def _enrich_route_metadata(self, route_data: Dict[str, Any]):
        """
        Update an existing route's metadata from 06a research.
        Applies to BOTH OSM and non-OSM routes.
        Updates: cooperative, jurisdiction, route_type.
        Uses confidence 0.6 so it beats OSM bridge (0.3-0.5)
        but loses to 06b (0.7+).
        """
        route_code = route_data.get("route_code", "")
        display_name = route_data.get("display_name", "")
        if not route_code:
            return

        try:
            with db_conn() as conn:
                with db_cursor(conn) as cur:
                    # Find existing route by ref or name
                    cur.execute(
                        """
                        SELECT r.route_id::text AS route_id
                        FROM route_prod.routes r
                        LEFT JOIN route_prod.route_semantics rs
                            ON rs.route_id = r.route_id
                        WHERE rs.route_ref = %s
                           OR r.route_name ILIKE %s
                        LIMIT 1
                        """,
                        (route_code, f"%{display_name}%"),
                    )
                    row = cur.fetchone()
                    if not row:
                        return

                    route_id = row["route_id"]

                    cur.execute(
                        """
                        INSERT INTO catalog.route_semantics (
                            route_id, operator, route_short_name,
                            route_long_name, jurisdiction,
                            evidence_source, confidence,
                            created_at, updated_at
                        ) VALUES (
                            %s::uuid, %s, %s, %s, %s,
                            'deep_research_06a', 0.6,
                            NOW(), NOW()
                        )
                        ON CONFLICT (route_id) DO UPDATE SET
                            operator = CASE
                                WHEN catalog.route_semantics.confidence < 0.6
                                THEN COALESCE(
                                    EXCLUDED.operator,
                                    catalog.route_semantics.operator
                                )
                                ELSE catalog.route_semantics.operator
                            END,
                            jurisdiction = CASE
                                WHEN catalog.route_semantics.confidence < 0.6
                                THEN COALESCE(
                                    EXCLUDED.jurisdiction,
                                    catalog.route_semantics.jurisdiction
                                )
                                ELSE catalog.route_semantics.jurisdiction
                            END,
                            confidence = GREATEST(
                                catalog.route_semantics.confidence,
                                EXCLUDED.confidence
                            ),
                            updated_at = NOW()
                        """,
                        (
                            route_id,
                            route_data.get("cooperative", ""),
                            route_code,
                            display_name,
                            route_data.get("jurisdiction", "DMQ"),
                        ),
                    )
                    conn.commit()
        except Exception:
            pass  # non-critical enrichment

    def _generate_schedule_request_with_context(self) -> str:
        """
        Generate the 06b route list with context: which routes came from
        OSM vs research.
        """
        canton_bbox = self._get_canton_bbox()
        if not canton_bbox:
            return "Drop 06b schedules (no bbox — list routes manually)."

        route_list: List[str] = []
        try:
            s, w, n, e = canton_bbox
            with db_conn() as conn:
                with db_cursor(conn) as cur:
                    cur.execute(
                        """
                        SELECT r.route_name,
                               COALESCE(rs.route_ref, '?') AS route_ref,
                               COALESCE(cs.operator, '') AS operator,
                               r.source
                        FROM route_prod.routes r
                        LEFT JOIN route_prod.route_semantics rs
                            ON rs.route_id = r.route_id
                        LEFT JOIN catalog.route_semantics cs
                            ON cs.route_id = r.route_id
                        WHERE ST_Intersects(
                            r.geom,
                            ST_MakeEnvelope(%s, %s, %s, %s, 4326)
                        )
                        ORDER BY rs.route_ref, r.route_name
                        """,
                        (w, s, e, n),
                    )
                    for i, row in enumerate(cur.fetchall(), 1):
                        ref = row["route_ref"]
                        name = row["route_name"] or "unnamed"
                        source = (
                            "(OSM)"
                            if "osm" in (row.get("source") or "").lower()
                            else "(research)"
                        )
                        route_list.append(f"  {i}. {ref} {name} {source}")
        except Exception:
            return "Drop 06b schedules (DB query failed — list routes manually)."

        lines = [
            f"Drop 06b schedules for these {len(route_list)} routes:\n",
        ]
        lines.extend(route_list)
        lines.append(
            "\nRoutes marked (OSM) have geometry but zero operational data."
        )
        lines.append(
            "Routes marked (research) were constructed from Deep Research seeds."
        )
        lines.append(
            "ALL routes need: headways, runtimes (min/typical/max), service days."
        )
        return "\n".join(lines)

    def _identify_gaps(self, research: Dict[str, Any]) -> Dict[str, Any]:
        """
        Compare known cooperatives (from research) vs built routes.
        Returns {missing_routes: [...], missing_coops: [...]}.
        """
        coops_file = os.path.join(self.canton_dir, "cooperatives.json")
        if not os.path.exists(coops_file):
            return {"missing_routes": [], "missing_coops": []}

        with open(coops_file) as f:
            coops = json.load(f)

        canton_bbox = self._get_canton_bbox()
        if not canton_bbox:
            return {"missing_routes": [], "missing_coops": []}

        # Count built routes per operator
        built_by_operator: Dict[str, int] = {}
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                s, w, n, e = canton_bbox
                cur.execute(
                    """
                    SELECT
                        COALESCE(tags->>'operator', 'unknown') AS operator,
                        COUNT(*) AS cnt
                    FROM route_raw.osm_route_relations
                    WHERE ST_Intersects(
                        geom,
                        ST_MakeEnvelope(%s, %s, %s, %s, 4326)
                    )
                    GROUP BY 1
                    """,
                    (w, s, e, n),
                )
                for row in cur.fetchall():
                    built_by_operator[row["operator"].lower()] = row["cnt"]

        missing_routes: List[str] = []
        missing_coops: List[Dict[str, Any]] = []

        for coop in coops:
            coop_name = coop.get("name", "")
            expected = coop.get("estimated_routes", 0)
            found = built_by_operator.get(coop_name.lower(), 0)
            gap = max(0, expected - found)

            if gap > 0:
                missing_routes.extend([coop_name] * gap)
                missing_coops.append(
                    {
                        "name": coop_name,
                        "expected": expected,
                        "found": found,
                        "estimated_missing": gap,
                    }
                )

        return {"missing_routes": missing_routes, "missing_coops": missing_coops}

    # ════════════════════════════════════════════════════════════
    # Research → Constructor V2 bridge (Skill 03 §Operation 1)
    # ════════════════════════════════════════════════════════════
    #
    # Wires 06a research entries into the canonical Phase 3 pipeline:
    #     create_route_job(province) → DualCatalogContext → intake_typed_seed
    #     → run_discovery_pipeline → route_work.discovery_runs upsert
    #     → route_work.geometry_candidate_sets + geometry_candidates
    #     → route_prod.routes INSERT (province threaded per Skill 11 §7)
    #
    # Replaces the historical stub (pre-2026-04-09) that blocked every
    # non-OSM canton from completing cycle_1. Province is threaded through
    # every INSERT — never relies on the column DEFAULT.

    @staticmethod
    def _research_route_to_typed_entry(rr: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convert a 06a research_route dict (with nested seed_catalog) into the
        flat shape that ``intake_typed_seed`` consumes:

            {
              "route_name": str,
              "cooperative": str,
              "route_type": str,
              "anchor_a_hint": str,
              "anchor_b_hint": str,
              "intermediate_hints": [str, ...],
              "sequence_seed_typed": [ { label, anchor_lat, anchor_lon, ... } ],
              "corridor_description": str,
              "province": <slug or None>,
            }
        """
        seed = rr.get("seed_catalog") or {}
        origin = seed.get("terminus_origin") or {}
        destination = seed.get("terminus_destination") or {}
        intermediates = list(seed.get("intermediate_stops") or [])

        tokens: List[Dict[str, Any]] = []
        if origin.get("name"):
            tokens.append(
                {
                    "label": origin["name"],
                    "anchor_lat": origin.get("lat"),
                    "anchor_lon": origin.get("lon"),
                    "terminus_type": "terminus",
                    "role": "origin",
                    "kind": "stop_candidate",
                    "position": 1,
                }
            )
        for idx, stop in enumerate(intermediates, start=2):
            if not stop.get("name"):
                continue
            tokens.append(
                {
                    "label": stop["name"],
                    "anchor_lat": stop.get("lat"),
                    "anchor_lon": stop.get("lon"),
                    "terminus_type": stop.get("type", "intermediate"),
                    "role": "intermediate",
                    "kind": "stop_candidate",
                    "position": idx,
                }
            )
        if destination.get("name"):
            tokens.append(
                {
                    "label": destination["name"],
                    "anchor_lat": destination.get("lat"),
                    "anchor_lon": destination.get("lon"),
                    "terminus_type": "terminus",
                    "role": "destination",
                    "kind": "stop_candidate",
                    "position": len(tokens) + 1,
                }
            )

        intermediate_hints = [
            s.get("name") for s in intermediates if s.get("name")
        ]

        return {
            "route": rr.get("route_code") or rr.get("display_name") or "",
            "route_name": rr.get("display_name") or rr.get("route_code") or "",
            "cooperative": rr.get("cooperative") or "",
            "route_type": rr.get("route_type") or "conventional",
            "anchor_a_hint": origin.get("name") or "",
            "anchor_b_hint": destination.get("name") or "",
            "intermediate_hints": intermediate_hints,
            "sequence_seed_typed": tokens,
            "corridor_description": (
                rr.get("corridor_description")
                or rr.get("notes")
                or ""
            ),
            # province propagates into intake_typed_seed per Camino D PIEZA 7
            # when the caller doesn't pass it explicitly.
            "province": None,  # caller injects it
        }

    def _build_catalog_ctx_from_research(
        self, research: Dict[str, Any]
    ) -> DualCatalogContext:
        """
        Materialize a DualCatalogContext in-memory from a 06a file whose
        routes each carry a nested ``geography_catalog`` dict. This avoids
        the need to write two separate catalog files — we already have the
        data per-route in the inventory.

        The resulting context carries province/unit_name for downstream
        province-aware helpers (Camino D + Skill 11 §7).
        """
        province = self.province  # already lowercased in __init__
        unit_name = (
            research.get("unit_name")
            or research.get("canton")
            or self.canton
        )

        area_definitions: Dict[str, AreaDefinition] = {}
        route_geographies: Dict[str, RouteGeography] = {}
        seed_routes: List[Dict[str, Any]] = []

        for rr in research.get("routes", []):
            route_name = rr.get("display_name") or rr.get("route_code") or ""
            if not route_name:
                continue
            typed_entry = self._research_route_to_typed_entry(rr)
            seed_routes.append(typed_entry)

            geo = rr.get("geography_catalog") or {}
            if not geo:
                continue

            # Area definitions — each route's geography_catalog may carry a
            # local `area_definitions` dict. Merge into the shared pool; later
            # entries win on duplicate keys (which is fine because T3 seeds
            # are authored coherently by a single research pass).
            for key, raw in (geo.get("area_definitions") or {}).items():
                area_definitions[key] = AreaDefinition(
                    key=key,
                    description=str(raw.get("description") or ""),
                    approx_center=raw.get("approx_center"),
                    approx_bbox=raw.get("approx_bbox"),
                    sub_sectors=list(raw.get("sub_sectors") or []),
                    key_landmarks=list(raw.get("key_landmarks") or []),
                    corridor_waypoints=list(raw.get("corridor_waypoints") or []),
                )

            route_geographies[route_name] = RouteGeography(
                route_name=route_name,
                cooperative=rr.get("cooperative") or "",
                route_type=rr.get("route_type") or "",
                expected_distance_km=geo.get("expected_distance_km"),
                must_pass_through_areas_ordered=list(
                    geo.get("must_pass_through_areas_ordered")
                    or geo.get("required_areas")
                    or []
                ),
                must_NOT_enter=list(
                    geo.get("must_NOT_enter")
                    or geo.get("forbidden_areas")
                    or []
                ),
                expected_key_waypoints=list(geo.get("expected_key_waypoints") or []),
                expected_arterials=list(geo.get("expected_arterials") or []),
                notes=str(geo.get("notes") or ""),
            )

        return DualCatalogContext(
            seed_routes=seed_routes,
            resolution_rules=dict(research.get("sequence_resolution_rules") or {}),
            area_definitions=area_definitions,
            route_geographies=route_geographies,
            seed_catalog_path=f"<inmem:{self.canton}_06a_seed>",
            geography_catalog_path=f"<inmem:{self.canton}_06a_geography>",
            province=province,
            unit_name=unit_name,
        )

    def _build_from_research(
        self,
        research_route: Dict[str, Any],
        *,
        catalog_ctx: Optional[DualCatalogContext] = None,
        conn=None,
    ) -> bool:
        """
        Build one route from a 06a Deep Research seed entry, following the
        canonical Skill 03 §Operation 1 chain. Threads ``self.province``
        through every INSERT (Skill 11 §7).

        Returns True iff a row landed in route_prod.routes for this route.
        """
        rr = research_route
        label = (
            rr.get("route_code")
            or rr.get("display_name")
            or rr.get("route")
            or "?"
        )

        if not rr.get("seed_catalog"):
            self.report(f"  SKIP {label}: no seed_catalog")
            return False

        # ── 1. Build the typed catalog entry and seed ──
        entry = self._research_route_to_typed_entry(rr)
        try:
            typed_seed = intake_typed_seed(
                entry,
                rules=(catalog_ctx.resolution_rules if catalog_ctx else None),
                province=self.province,
            )
        except Exception as exc:
            self.report(f"  FAIL {label}: intake_typed_seed raised: {exc}")
            return False

        # ── 2. Create the route_job row (province required per Skill 11 §7) ──
        own_conn = False
        if conn is None:
            conn = db_conn().__enter__()
            own_conn = True
        try:
            try:
                route_id = create_route_job(
                    conn,
                    created_by=f"canton_pipeline:{self.canton}",
                    notes=(
                        f"06a research build: code={rr.get('route_code','?')} "
                        f"name={rr.get('display_name','?')} "
                        f"coop={rr.get('cooperative','?')}"
                    ),
                    province=(self.province or "sample_region"),
                )
            except ValueError as exc:
                # create_route_job refuses empty province (Skill 11 §7).
                self.report(f"  FAIL {label}: {exc}")
                return False

            # ── 3. Run the discovery pipeline (stop grounding + corridor +
            #       skeleton + geometry, all with province-aware guards) ──
            artifact_dir = os.path.join(
                self.canton_dir,
                "constructor_artifacts",
                f"cycle1_{self.canton}",
            )
            os.makedirs(artifact_dir, exist_ok=True)

            try:
                _a2_inputs = A2RunInputs(
                    route_id=str(route_id),
                    route_code=str(rr.get("route_code") or typed_seed.route_name or ""),
                    unit=self.canton,
                    province=(self.province or "sample_region"),
                    termini=extract_termini(typed_seed),
                    osm_relation_id=(rr.get("_meta") or {}).get("osm_relation_id"),
                    grounded_stop_coords=(),
                    review_root=Path(artifact_dir),
                    research_queue_root=Path("workspace/research_queue"),
                )
                summary = run_discovery_pipeline(
                    typed_seed,
                    artifact_dir=artifact_dir,
                    conn=conn,
                    catalog_ctx=catalog_ctx,
                    enable_a2=_A2_ENABLED,
                    a2_inputs=_a2_inputs,
                )
            except Exception as exc:
                self.report(
                    f"  FAIL {label}: run_discovery_pipeline raised: {exc}"
                )
                return False

            summary_dict = summary.to_dict()
            status = summary_dict.get("status") or "failed"

            # ── 4. Upsert into route_work.discovery_runs (province threaded
            #       by Camino D PIEZA 7 via _summary_to_db_params) ──
            legacy_seed = typed_seed_to_route_seed(typed_seed)
            try:
                params = _discovery_summary_to_db_params(
                    legacy_seed,
                    summary_dict,
                    catalog_name=f"{self.canton}_06a_inmem",
                    catalog_index=0,
                    entry=entry,
                    scoring_mode="ensemble",
                    province=self.province,
                )
                with db_cursor(conn) as cur:
                    cur.execute(_DISCOVERY_RUNS_UPSERT_SQL, params)
            except Exception as exc:
                # Non-fatal: we still try to persist to route_prod if the
                # summary has usable geometry + stops.
                self.report(
                    f"  WARN {label}: discovery_runs upsert failed: {exc}"
                )

            # ── 5. Promotion gate: require completed + geometry + >=2 stops ──
            geometry = summary_dict.get("geometry") or {}
            skeleton = summary_dict.get("skeleton") or {}
            geom_geojson = geometry.get("geometry_geojson")
            ordered_stops = list(skeleton.get("ordered_stops") or [])
            stop_ids = [
                s.get("stop_id")
                for s in ordered_stops
                if isinstance(s, dict) and s.get("stop_id")
            ]

            if status != "completed":
                self.report(
                    f"  BLOCKED {label}: discovery status={status} "
                    f"(see {artifact_dir})"
                )
                return False
            if not geom_geojson:
                self.report(
                    f"  BLOCKED {label}: no geometry_geojson in summary"
                )
                return False
            if len(stop_ids) < 2:
                self.report(
                    f"  BLOCKED {label}: only {len(stop_ids)} grounded stops "
                    f"(need >=2)"
                )
                return False

            # ── 6. Promote to route_prod.routes (province threaded) ──
            try:
                self._promote_summary_to_route_prod(
                    conn=conn,
                    route_id=route_id,
                    route_name=entry["route_name"],
                    cooperative=entry["cooperative"],
                    geom_geojson=geom_geojson,
                    geom_length_km=float(geometry.get("total_length_km") or 0),
                    geom_confidence=float(geometry.get("geometry_confidence") or 0),
                    seq_confidence=float(
                        (summary_dict.get("metrics") or {}).get(
                            "sequence_confidence"
                        )
                        or skeleton.get("sequence_confidence")
                        or 0
                    ),
                    stop_node_ids=stop_ids,
                    ordered_stops=ordered_stops,
                    valhalla_meta=geometry.get("valhalla_meta"),
                    province=(self.province or "sample_region"),
                    research_code=rr.get("route_code") or "",
                )
            except Exception as exc:
                self.report(
                    f"  FAIL {label}: promote to route_prod raised: {exc}"
                )
                return False

            conn.commit()
            self.report(
                f"  BUILT {label}: {len(stop_ids)} stops, "
                f"{geometry.get('total_length_km') or 0:.1f} km "
                f"(province={self.province or 'sample_region'})"
            )
            return True
        finally:
            if own_conn:
                try:
                    conn.__exit__(None, None, None)  # type: ignore[attr-defined]
                except Exception:
                    try:
                        conn.close()  # type: ignore[attr-defined]
                    except Exception:
                        pass

    def _promote_summary_to_route_prod(
        self,
        *,
        conn,
        route_id,
        route_name: str,
        cooperative: str,
        geom_geojson: Any,
        geom_length_km: float,
        geom_confidence: float,
        seq_confidence: float,
        stop_node_ids: List[str],
        ordered_stops: List[Dict[str, Any]],
        valhalla_meta: Optional[Dict[str, Any]],
        province: str,
        research_code: str,
    ) -> None:
        """
        Create route_work.geometry_candidate_sets + geometry_candidates rows
        for this route, run the HADES enforcer gate, and on auto_accept
        INSERT into route_prod.routes with all four audit kwargs populated.
        On queue_for_approval the route lands in route_prod.approval_queue
        instead. On reject the caller's transaction is rolled back via a
        raised exception.

        The INSERT sets ``province`` explicitly (Skill 11 §7). The enforcer
        gate runs inside the same transaction as the earlier route_job /
        discovery_runs writes so a rejection rolls the whole thing back.
        """
        geom_geojson_str = json.dumps(geom_geojson)
        geom_set_id = str(uuid.uuid4())
        seq_set_id = str(uuid.uuid4())
        seq_candidate_id = str(uuid.uuid4())
        approved_by = f"canton_pipeline:{self.canton}"

        # Convert geometry to WKT for the insert_geometry_candidate helper.
        # Prefer the geojson LineString; fall back to straight stop polyline
        # only if the geojson is missing/empty (the promotion gate in the
        # caller already guarantees it isn't, but the check here is cheap).
        coords = []
        if isinstance(geom_geojson, dict):
            coords = geom_geojson.get("coordinates") or []
        if len(coords) < 2:
            raise RuntimeError(
                f"_promote_summary_to_route_prod: geom_geojson has <2 coords "
                f"for route {route_id}"
            )
        linestring_wkt = "LINESTRING(" + ",".join(
            f"{float(c[0])} {float(c[1])}" for c in coords
        ) + ")"

        # Stop coords for the enforcer: (lat, lon) per enforcer API.
        stop_coords_latlon: List[Tuple[float, float]] = [
            (float(s.get("lat")), float(s.get("lon")))
            for s in (ordered_stops or [])
            if isinstance(s, dict) and s.get("lat") is not None and s.get("lon") is not None
        ]
        # Corridor coords for the enforcer: (lon, lat).
        corridor_coords_lonlat: List[Tuple[float, float]] = [
            (float(c[0]), float(c[1])) for c in coords
        ]

        valhalla_meta = valhalla_meta or {}
        valhalla_response_hash = valhalla_meta.get("response_hash") if isinstance(valhalla_meta, dict) else None

        with db_cursor(conn) as cur:
            # 1. route_work.stop_sequence_candidate_sets — the approved canonical
            #    stop sequence for this route. approve.py requires this row to
            #    exist so FK chains from geometry_candidates → sequence_approvals
            #    → route_approvals can close.
            cur.execute(
                """
                INSERT INTO route_work.stop_sequence_candidate_sets (
                    set_id, route_id, generator_version, notes, created_by
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    seq_set_id,
                    str(route_id),
                    "canton_pipeline_06a_v1",
                    f"{self.canton} cycle_1 06a discovery skeleton ({research_code})",
                    approved_by,
                ),
            )

            # 2. route_work.stop_sequence_candidates — rank 1 = the discovery
            #    pipeline's ordered stop skeleton, mirrored from stop_node_ids.
            cur.execute(
                """
                INSERT INTO route_work.stop_sequence_candidates (
                    candidate_id, set_id, rank, stop_node_ids,
                    matched_stops, metrics
                ) VALUES (%s, %s, 1, %s::uuid[], %s, %s)
                """,
                (
                    seq_candidate_id,
                    seq_set_id,
                    stop_node_ids,
                    len(stop_node_ids),
                    json.dumps(
                        {
                            "source": "canton_pipeline_06a",
                            "sequence_confidence": seq_confidence,
                            "research_code": research_code,
                        }
                    ),
                ),
            )

            # 3. route_work.geometry_candidate_sets — note linkage to seq_set_id
            cur.execute(
                """
                INSERT INTO route_work.geometry_candidate_sets (
                    set_id, route_id, generator_version, notes, created_by,
                    stop_sequence_set_id
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    geom_set_id,
                    str(route_id),
                    "canton_pipeline_06a_v1",
                    f"{self.canton} cycle_1 06a research build ({research_code})",
                    approved_by,
                    seq_set_id,
                ),
            )

            # 4. route_work.geometry_candidates — routed through the helper
            #    so the valhalla_request + response_hash land in the right
            #    JSONB columns. This closes Prompt-7b gap #3: prior manual
            #    INSERT silently dropped valhalla metadata.
            geom_candidate_uuid = insert_geometry_candidate(
                conn,
                set_id=uuid.UUID(geom_set_id),
                stop_sequence_candidate_id=uuid.UUID(seq_candidate_id),
                engine="discovery_pipeline",
                params={
                    "source": "canton_pipeline_06a",
                    "canton": self.canton,
                    "province": province,
                    "research_code": research_code,
                },
                linestring_wkt=linestring_wkt,
                score=float(geom_confidence or 0),
                length_m=float(geom_length_km or 0) * 1000,
                # The canton pipeline's discovery summary does not compute
                # per-stop distance-to-corridor metrics; leave as 0.0 and
                # annotate in metrics (approve.py reads metrics JSON, not
                # these two columns).
                avg_stop_dist_m=0.0,
                max_stop_dist_m=0.0,
                metrics={
                    "source": "canton_pipeline_06a",
                    "canton": self.canton,
                    "province": province,
                    "research_code": research_code,
                    "note": "avg/max stop dist not computed at this stage",
                },
                valhalla_request=valhalla_meta or None,
                valhalla_response_hash=valhalla_response_hash,
            )
            geom_candidate_id = str(geom_candidate_uuid)

            # 5. route_work.sequence_approvals — marks the canonical sequence
            #    as approved. approve.py refuses to promote geometry otherwise.
            cur.execute(
                """
                INSERT INTO route_work.sequence_approvals (
                    route_id, stop_sequence_set_id,
                    chosen_stop_sequence_candidate_id,
                    approval_status, approved_at, approved_by, notes
                ) VALUES (%s, %s, %s, 'approved', now(), %s, %s)
                ON CONFLICT (route_id) DO UPDATE SET
                    stop_sequence_set_id = EXCLUDED.stop_sequence_set_id,
                    chosen_stop_sequence_candidate_id = EXCLUDED.chosen_stop_sequence_candidate_id,
                    approval_status = 'approved',
                    approved_at = now(),
                    approved_by = EXCLUDED.approved_by,
                    notes = EXCLUDED.notes
                """,
                (
                    str(route_id),
                    seq_set_id,
                    seq_candidate_id,
                    approved_by,
                    f"auto-approved by canton_pipeline cycle_1 ({research_code})",
                ),
            )

            # 6. route_work.route_approvals — the geometry approval ledger
            cur.execute(
                """
                INSERT INTO route_work.route_approvals (
                    route_id, chosen_geometry_candidate_id,
                    chosen_stop_sequence_candidate_id,
                    approved_by, notes
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (route_id) DO UPDATE SET
                    chosen_geometry_candidate_id = EXCLUDED.chosen_geometry_candidate_id,
                    chosen_stop_sequence_candidate_id = EXCLUDED.chosen_stop_sequence_candidate_id,
                    approved_at = now(),
                    approved_by = EXCLUDED.approved_by,
                    notes = EXCLUDED.notes
                """,
                (
                    str(route_id),
                    geom_candidate_id,
                    seq_candidate_id,
                    approved_by,
                    f"auto-approved by canton_pipeline cycle_1 ({research_code})",
                ),
            )

            # 6.5. HADES enforcer gate — mandatory between quality gate and
            #      route_prod write. Runs both enforcers, hands the reports
            #      to the policy engine, returns a verdict. `zone=None` lets
            #      stop_coverage_enforcer.infer_zone classify from the route
            #      length + stop count; hardcoding "urban_dense" was the
            #      2026-04-21 pre-close mistake we explicitly rejected.
            enforcer_ctx = RouteContext(
                route_code=str(route_id),
                coords=corridor_coords_lonlat,
                stop_coords=stop_coords_latlon,
                stop_ids=list(stop_node_ids or []),
                zone=None,
                version=1,
                unit_id=f"{self.province or 'unknown'}:{self.canton}",
            )
            enforcer_result: EnforcerResult = run_phase3_enforcers(
                enforcer_ctx,
                policy_profile=self.policy_profile,
            )

            if enforcer_result.policy_decision == "reject_send_to_phase2":
                # Rejection rolls the whole transaction back via the raise.
                raise RuntimeError(
                    f"Phase 3 enforcer REJECT route={route_id} "
                    f"profile={self.policy_profile} "
                    f"reasons={enforcer_result.decision_reasons}"
                )

            if enforcer_result.policy_decision == "queue_for_approval":
                cur.execute(
                    """
                    INSERT INTO route_prod.approval_queue (
                        route_code, version, policy_profile,
                        decision_reasons, policy_flags,
                        geometry_report, stop_coverage_report,
                        proposed_stops, proposed_shape,
                        dr_queries_queued, dr_queries_deferred,
                        crashed, crash_payload
                    ) VALUES (
                        %s, %s, %s,
                        %s::jsonb, %s::jsonb,
                        %s::jsonb, %s::jsonb,
                        %s::jsonb, %s::jsonb,
                        %s::jsonb, %s::jsonb,
                        %s, %s::jsonb
                    )
                    """,
                    (
                        str(route_id),
                        1,
                        self.policy_profile,
                        json.dumps(list(enforcer_result.decision_reasons)),
                        json.dumps(dict(enforcer_result.policy_flags)),
                        json.dumps(enforcer_result.geometry_report),
                        json.dumps(enforcer_result.stop_coverage_report),
                        json.dumps(list(stop_node_ids or [])),
                        geom_geojson_str,
                        json.dumps(list(enforcer_result.dr_queries_queued)),
                        json.dumps(list(enforcer_result.dr_queries_deferred)),
                        bool(enforcer_result.crashed),
                        json.dumps(enforcer_result.crash_payload) if enforcer_result.crash_payload else None,
                    ),
                )
                # Stop the promotion path — no write to route_prod.routes.
                return

            # 7. route_prod.routes — province threaded explicitly (Skill 11 §7),
            #    canonical sequence flags set, chosen_stop_sequence_candidate_id
            #    linked so the approval chain is internally consistent. Routed
            #    through the canonical writer wrapper (datamind_console.persistence).
            #    NOTE: legacy SQL used `now()` for sequence_approved_at; we pass a
            #    Python datetime here — same transaction, millisecond-level delta.
            quality_gate_passed_at = datetime.now(timezone.utc)
            result = write_to_route_prod(
                route_code=str(route_id),
                route_data={
                    "route_id": str(route_id),
                    "province": province,
                    "source": f"canton_pipeline_06a_{self.canton}",
                    "chosen_geometry_candidate_id": geom_candidate_id,
                    "chosen_stop_sequence_candidate_id": seq_candidate_id,
                    "canonical_sequence_ready": True,
                    "sequence_approved_at": datetime.utcnow(),
                    "sequence_approved_by": approved_by,
                    "route_name": route_name,
                    "naming_confidence": round(
                        float(geom_confidence or 0) * 0.5
                        + float(seq_confidence or 0) * 0.5,
                        3,
                    ),
                    "direction_semantics": {
                        "cooperative": cooperative,
                        "geometry_confidence": geom_confidence,
                        "sequence_confidence": seq_confidence,
                        "research_code": research_code,
                        "canton": self.canton,
                    },
                },
                stops=list(stop_node_ids or []),
                shape={"geojson": geom_geojson_str},
                source_type="canton_pipeline_06a",
                pipeline_version=_PIPELINE_VERSION,
                valhalla_request=valhalla_meta or None,
                geometry_enforcer_report=enforcer_result.geometry_report,
                stop_coverage_report=enforcer_result.stop_coverage_report,
                quality_gate_passed_at=quality_gate_passed_at,
                conn=conn,
                mode="upsert",
            )
            if not result.success:
                raise RuntimeError(
                    f"route_prod write failed for {route_id}: {result.error}"
                )

            # 8. route_raw.route_jobs.status = 'approved' (mirrors approve.py §4)
            cur.execute(
                "UPDATE route_raw.route_jobs SET status = 'approved' WHERE route_id = %s",
                (str(route_id),),
            )

    def _enrich_existing_route(self, match: Dict[str, Any]):
        """Enrich an existing OSM-built route with Deep Research data."""
        # Stub: no-op until enrichment logic is wired
        pass

    def _ingest_all_catalogs(self, research: Dict[str, Any]) -> Dict[str, int]:
        """Ingest all catalog tables from 06b research. Returns counts."""
        counts: Dict[str, int] = {"total": 0, "semantics": 0, "schedules": 0, "service_days": 0, "layovers": 0}

        for key in ["semantics", "schedules", "service_days", "layovers"]:
            items = research.get(key, [])
            counts[key] = len(items)
            counts["total"] += len(items)
            # Stub: actual DB ingestion goes here

        self.report("  (Catalog ingestion: stub -- wire to catalog ingest pipeline)")
        return counts

    def _estimate_all_runtimes(self) -> Dict[str, Any]:
        """Run Runtime Lab for all unbound routes."""
        # Stub: returns empty stats until Runtime Lab is wired
        self.report("  (Runtime estimation: stub)")
        return {"estimated": 0, "bound": 0, "avg_confidence": "N/A"}

    def _compute_all_vehicle_blocks(self) -> Dict[str, int]:
        """Compute vehicle blocks for all routes."""
        # Stub
        self.report("  (Vehicle blocks: stub)")
        return {"updated": 0}

    def _compile_gtfs(self) -> Dict[str, Any]:
        """Compile + validate + package GTFS for the entire canton."""
        # Delegate to existing export_gtfs.py + validate_gtfs.py
        scripts_dir = _ROOT / "scripts"
        export_script = scripts_dir / "export_gtfs.py"
        validate_script = scripts_dir / "validate_gtfs.py"

        canton_bbox = self._get_canton_bbox()
        if not canton_bbox or not export_script.exists():
            self.report("  (GTFS compilation: stub -- scripts not found)")
            return {
                "routes": 0,
                "trips": 0,
                "stop_times": 0,
                "validation": "SKIP",
            }

        output_dir = os.path.join(self.canton_dir, "gtfs")
        os.makedirs(output_dir, exist_ok=True)

        s, w, n, e = canton_bbox
        cmd = [
            sys.executable,
            str(export_script),
            "--bbox",
            f"{s},{w},{n},{e}",
            "--output",
            output_dir,
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(_ROOT)

        result = subprocess.run(
            cmd,
            cwd=str(_ROOT),
            env=env,
            text=True,
            capture_output=True,
            timeout=300,
        )

        if result.returncode != 0:
            self.report(f"  GTFS export failed: {(result.stderr or '')[-200:]}")
            return {
                "routes": 0,
                "trips": 0,
                "stop_times": 0,
                "validation": "FAIL",
            }

        # Parse counts from output
        import re

        stats: Dict[str, Any] = {
            "routes": 0,
            "trips": 0,
            "stop_times": 0,
            "validation": "UNKNOWN",
        }
        for key in ["routes", "trips", "stop_times"]:
            m = re.search(rf"{key}:\s*(\d+)", result.stdout or "", re.IGNORECASE)
            if m:
                stats[key] = int(m.group(1))

        # Validate
        if validate_script.exists():
            val_result = subprocess.run(
                [sys.executable, str(validate_script), "--input", output_dir],
                cwd=str(_ROOT),
                env=env,
                text=True,
                capture_output=True,
                timeout=120,
            )
            stats["validation"] = (
                "PASS" if val_result.returncode == 0 else "FAIL"
            )

        return stats

    def _apply_default_fares(self):
        """Apply standard Ecuador urban fare ($0.30 base)."""
        # Stub: insert default fare_attributes + fare_rules
        self.report("  (Default fares: stub -- $0.30 base urban fare)")

    def _ingest_fares(self, input_file: str):
        """Ingest custom fares from 06c research."""
        with open(input_file) as f:
            fares = json.load(f)
        self.report(f"  Custom fare entries: {len(fares.get('fares', []))}")
        # Stub: actual DB ingestion goes here

    def _get_canton_bbox(self) -> Optional[BBox]:
        """Read canton bbox from stored infrastructure data."""
        bbox_file = os.path.join(self.canton_dir, "canton_bbox.json")
        if not os.path.exists(bbox_file):
            return None
        with open(bbox_file) as f:
            data = json.load(f)
        bbox = data.get("bbox")
        if not bbox or len(bbox) != 4:
            return None
        return tuple(bbox)  # type: ignore[return-value]

    def _generate_schedule_request(self) -> str:
        """Generate the route list for 06b prompt."""
        canton_bbox = self._get_canton_bbox()
        if not canton_bbox:
            return "Drop 06b schedules (no bbox -- list routes manually)."

        route_list: List[str] = []
        try:
            s, w, n, e = canton_bbox
            with db_conn() as conn:
                with db_cursor(conn) as cur:
                    cur.execute(
                        """
                        SELECT
                            r.route_name,
                            rs.route_ref
                        FROM route_prod.routes r
                        LEFT JOIN route_prod.route_semantics rs
                            ON rs.route_id = r.route_id
                        WHERE ST_Intersects(
                            r.geom,
                            ST_MakeEnvelope(%s, %s, %s, %s, 4326)
                        )
                        ORDER BY r.route_name
                        """,
                        (w, s, e, n),
                    )
                    for i, row in enumerate(cur.fetchall(), 1):
                        ref = row.get("route_ref") or "?"
                        name = row.get("route_name") or "unnamed"
                        route_list.append(f"  {i}. {ref} {name}")
        except Exception:
            return "Drop 06b schedules (DB query failed -- list routes manually)."

        header = f"Drop 06b schedules for these {len(route_list)} routes:"
        return header + "\n" + "\n".join(route_list)


# ═════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Canton Pipeline Orchestrator -- batch processing discipline"
    )
    parser.add_argument("--canton", required=True, help="Canton name (e.g. cayambe)")
    parser.add_argument(
        "--cycle",
        type=int,
        required=True,
        choices=[0, 1, 2, 3],
        help="Cycle number (0=context, 1=gap-fill, 2=schedules, 3=fares)",
    )
    parser.add_argument("--input", help="Path to research JSON input file")
    parser.add_argument(
        "--use-default-fares",
        action="store_true",
        help="Apply standard Ecuador fares (cycle 3 only)",
    )
    parser.add_argument(
        "--province",
        type=str,
        default=None,
        help=(
            "Active province key (e.g. sample_region, sample_region_b, manabi). "
            "Defaults to the default province in "
            "workspace/config/supported_provinces.json when omitted."
        ),
    )
    parser.add_argument(
        "--policy-profile",
        type=str,
        default=None,
        choices=list(_VALID_POLICY_PROFILES),
        help=(
            "HADES enforcer policy profile. Overrides PHASE3_POLICY_PROFILE; "
            "defaults to 'balanced'."
        ),
    )
    args = parser.parse_args()

    # Validate
    if args.cycle in (0, 1, 2) and not args.input:
        parser.error(f"--input is required for cycle {args.cycle}")
    if args.cycle == 3 and not args.input and not args.use_default_fares:
        parser.error("Cycle 3 requires --input or --use-default-fares")

    pipeline = CantonPipeline(
        args.canton,
        province=args.province,
        policy_profile=args.policy_profile,
    )

    if args.cycle == 0:
        pipeline.cycle_0(args.input)
    elif args.cycle == 1:
        pipeline.cycle_1(args.input)
    elif args.cycle == 2:
        pipeline.cycle_2(args.input)
    elif args.cycle == 3:
        pipeline.cycle_3(args.input, args.use_default_fares)


if __name__ == "__main__":
    main()
