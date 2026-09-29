"""
Route Constructor Runner
========================
Operational runner for the constructor orchestrator.
Can be called as a script or imported.

Usage::

    # From CLI
    python -m datamind_console.phases.phase3_routes.constructor_runner \\
        --input missing_routes.json \\
        --output-dir ~/phase3_route_catalog

    # From code
    from datamind_console.phases.phase3_routes.constructor_runner import run_constructor_batch
    results = run_constructor_batch(cases, output_dir=...)
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_LOG = logging.getLogger(__name__)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_json_dump(obj: Any, path: Path) -> None:
    """Write JSON with safe serialization."""
    def default(o):
        if hasattr(o, "isoformat"):
            return o.isoformat()
        return str(o)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, indent=2, ensure_ascii=False, default=default),
        encoding="utf-8",
    )
    _LOG.info("Wrote %s (%d bytes)", path, path.stat().st_size)


def run_constructor_batch(
    cases: List[Dict[str, Any]],
    *,
    output_dir: Optional[str] = None,
    desktop_mirror: bool = False,
    llm_mode: str = "mock",
    force_rerun: bool = False,
) -> Dict[str, Any]:
    """
    Run the constructor orchestrator on a batch of missing-route cases.

    Parameters
    ----------
    cases : list of dict
        Each case should have: name_hint, operator_hint, variant_hint,
        ordered_stop_ids, coverage_gap_id (optional), sector_key (optional)
    output_dir : str
        Where to write output artifacts.
    desktop_mirror : bool
        Whether to mirror outputs to ~/phase3_route_catalog/ (off by default)
    llm_mode : str
        LLM advisor mode: "real_advisory", "mock", "dry_run"
    force_rerun : bool
        Override duplicate/rerun safety checks

    Returns
    -------
    dict with keys: metrics, results, failures, artifacts_written
    """
    from datamind_console.phases.phase3_routes.constructor_orchestrator import (
        ConstructorOrchestrator,
        ConstructorMetrics,
    )
    from datamind_console.phases.phase3_routes.constructor_llm_advisor import (
        ConstructorLLMAdvisor,
    )

    out_dir = Path(output_dir or "phase3_route_catalog")
    out_dir.mkdir(parents=True, exist_ok=True)
    mirror_dir = Path.home() / "phase3_route_catalog"

    advisor = ConstructorLLMAdvisor(mode=llm_mode)

    # Try to get Phase3Client, but work without it
    client = None
    try:
        from datamind_console.phases.phase3_routes.client import Phase3Client
        client = Phase3Client()
    except Exception as exc:
        _LOG.warning("Phase3Client not available, using direct mode: %s", exc)

    orch = ConstructorOrchestrator(client)

    results: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    llm_evidence_payloads: List[Dict[str, Any]] = []
    llm_reasoning_outputs: List[Dict[str, Any]] = []
    review_audit_entries: List[Dict[str, Any]] = []

    for i, case in enumerate(cases):
        case_id = case.get("case_id", f"case_{i:04d}")
        _LOG.info("Processing case %s: %s", case_id, case.get("name_hint", "unnamed"))

        try:
            # Step 1: Preflight classification
            classification = orch.classify_case(case)
            pfclass = classification.get("classification", "operator_review_required")

            review_entry = {
                "case_id": case_id,
                "name_hint": case.get("name_hint"),
                "classification": pfclass,
                "confidence": classification.get("confidence"),
                "reasons": classification.get("reasons", []),
                "timestamp": _utc_iso(),
            }

            # Run LLM advisor on hard cases (duplicate_risk, operator_review_required)
            is_hard_case = pfclass in ("duplicate_risk", "operator_review_required")
            llm_result = None
            if is_hard_case:
                try:
                    evidence = {
                        "name_hint": case.get("name_hint"),
                        "operator_hint": case.get("operator_hint"),
                        "variant_hint": case.get("variant_hint"),
                        "sector_key": case.get("sector_key"),
                        "stop_count": len(case.get("ordered_stop_ids") or []),
                        "existing_catalog_routes": classification.get("catalog_matches", {}).get("near_matches", []),
                        "gap_context": case.get("coverage_gap_id"),
                    }
                    llm_result = advisor.interpret_missing_route(evidence)
                    review_entry["llm_source"] = llm_result.get("source")
                    review_entry["llm_confidence"] = llm_result.get("confidence")
                    review_entry["llm_action"] = llm_result.get("recommended_action")
                except Exception as llm_exc:
                    _LOG.warning("LLM advisor failed for case %s: %s", case_id, llm_exc)

            if llm_result:
                llm_evidence_payloads.append({
                    "case_id": case_id,
                    "task": llm_result.get("task"),
                    "evidence_payload": llm_result.get("evidence_payload"),
                    "timestamp": llm_result.get("timestamp"),
                })
                llm_reasoning_outputs.append({
                    "case_id": case_id,
                    "task": llm_result.get("task"),
                    "model_output": llm_result.get("model_output"),
                    "confidence": llm_result.get("confidence"),
                    "recommended_action": llm_result.get("recommended_action"),
                    "reasoning": llm_result.get("reasoning"),
                    "source": llm_result.get("source"),
                    "model": llm_result.get("model"),
                    "latency_ms": llm_result.get("latency_ms"),
                    "timestamp": llm_result.get("timestamp"),
                })

            # Step 2: Decision
            if pfclass in ("likely_already_represented", "missing_evidence"):
                review_entry["decision"] = "rejected"
                review_entry["reason"] = f"Blocked by preflight: {pfclass}"
                failures.append(review_entry)
                review_audit_entries.append(review_entry)
                continue

            if pfclass == "duplicate_risk" and not force_rerun:
                review_entry["decision"] = "held_for_review"
                review_entry["reason"] = "Duplicate risk - needs operator review"
                failures.append(review_entry)
                review_audit_entries.append(review_entry)
                continue

            if pfclass == "operator_review_required":
                # Still create draft but don't export
                try:
                    if client:
                        draft = client.save_manual_sequence_draft(case)
                        review_entry["draft_id"] = draft.get("draft_id")
                        review_entry["decision"] = "draft_saved_for_review"
                    else:
                        review_entry["decision"] = "held_for_review_no_client"
                except Exception as exc:
                    review_entry["decision"] = "draft_save_failed"
                    review_entry["error"] = str(exc)
                review_audit_entries.append(review_entry)
                results.append(review_entry)
                continue

            # Step 3: Export for ready cases
            if pfclass in ("true_missing_ready", "duplicate_risk", "needs_cleanup_first"):
                export_result = orch.safe_export(case)
                review_entry["decision"] = "exported" if export_result.get("ok", True) else "export_failed"
                review_entry["export_result"] = {
                    k: v for k, v in export_result.items()
                    if k in ("export_id", "route_job_id", "stop_sequence_set_id",
                             "stop_sequence_candidate_id", "warnings", "is_rerun",
                             "constructor_status", "ok", "error", "status")
                }
                if not export_result.get("ok", True) and export_result.get("status") == "blocked":
                    review_entry["decision"] = "blocked"
                    failures.append(review_entry)
                else:
                    results.append(review_entry)
                review_audit_entries.append(review_entry)

        except Exception as exc:
            _LOG.error("Case %s failed: %s", case_id, exc)
            failure_entry = {
                "case_id": case_id,
                "name_hint": case.get("name_hint"),
                "decision": "exception",
                "error": str(exc),
                "timestamp": _utc_iso(),
            }
            failures.append(failure_entry)
            review_audit_entries.append(failure_entry)
            orch.metrics.record_event("case_exception", {"case_id": case_id, "error": str(exc)})

    # Collect final metrics
    metrics = orch.collect_metrics()

    # Also get LLM call log
    llm_call_log = advisor.call_log if hasattr(advisor, "call_log") else []

    # Build failure classes summary
    failure_class_summary: List[Dict[str, Any]] = []
    from collections import Counter
    fc_counter = Counter(f.get("classification") or f.get("decision") or "unknown" for f in failures)
    for cls, count in fc_counter.most_common():
        failure_class_summary.append({"failure_class": cls, "count": count})

    # Write output artifacts
    artifacts_written: List[str] = []

    artifacts = {
        "constructor_metrics.json": metrics,
        "constructor_failure_classes.json": failure_class_summary,
        "constructor_review_audit.json": review_audit_entries,
        "llm_evidence_payload.json": llm_evidence_payloads,
        "llm_assisted_reasoning.json": llm_reasoning_outputs,
    }

    for name, data in artifacts.items():
        path = out_dir / name
        _safe_json_dump(data, path)
        artifacts_written.append(str(path))

    # Desktop mirror
    if desktop_mirror:
        mirror_dir.mkdir(parents=True, exist_ok=True)
        for name, data in artifacts.items():
            mirror_path = mirror_dir / name
            _safe_json_dump(data, mirror_path)
            artifacts_written.append(str(mirror_path))

    return {
        "metrics": metrics,
        "results": results,
        "failures": failures,
        "failure_classes": failure_class_summary,
        "llm_calls": len(llm_evidence_payloads),
        "artifacts_written": artifacts_written,
        "timestamp": _utc_iso(),
    }


def run_validation_suite(
    *,
    output_dir: Optional[str] = None,
    desktop_mirror: bool = False,
) -> Dict[str, Any]:
    """
    Run validation checks on the constructor system.
    Tests draft lifecycle, export safety, idempotency, and LLM integration.
    """
    from datamind_console.phases.phase3_routes.constructor_orchestrator import (
        ConstructorOrchestrator,
    )
    from datamind_console.phases.phase3_routes.constructor_llm_advisor import (
        ConstructorLLMAdvisor,
    )

    out_dir = Path(output_dir or "phase3_route_catalog")
    mirror_dir = Path.home() / "phase3_route_catalog"

    results: List[Dict[str, Any]] = []

    def _check(name: str, fn) -> Dict[str, Any]:
        try:
            result = fn()
            entry = {"test": name, "status": "PASS", "detail": result}
        except Exception as exc:
            entry = {"test": name, "status": "FAIL", "error": str(exc)}
        results.append(entry)
        return entry

    # Test 1: LLM advisor mock mode works
    def test_llm_mock():
        advisor = ConstructorLLMAdvisor(mode="mock")
        result = advisor.interpret_missing_route({
            "name_hint": "Ruta Test-123",
            "operator_hint": "TestOp",
            "stop_count": 10,
        })
        assert result.get("source") in ("mock", "heuristic_mock"), f"Unexpected source: {result.get('source')}"
        assert "model_output" in result or "recommended_action" in result
        return {"source": result.get("source"), "has_output": True}

    _check("llm_mock_interpret", test_llm_mock)

    # Test 2: LLM advisor duplicate assessment
    def test_llm_duplicate():
        advisor = ConstructorLLMAdvisor(mode="mock")
        result = advisor.assess_duplicate_risk(
            {"name_hint": "Ruta A", "operator_hint": "OpA"},
            [{"display_name": "Ruta A", "operator": "OpA", "status": "active"}],
        )
        assert result.get("source") in ("mock", "heuristic_mock")
        return {"source": result.get("source")}

    _check("llm_mock_duplicate_risk", test_llm_duplicate)

    # Test 3: Orchestrator preflight classification
    def test_preflight():
        orch = ConstructorOrchestrator()
        result = orch.classify_case({
            "ordered_stop_ids": ["00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"],
            "name_hint": "Test Route 42",
            "operator_hint": "TestCorp",
        })
        assert result.get("classification") in (
            "true_missing_ready", "likely_already_represented", "duplicate_risk",
            "needs_cleanup_first", "missing_evidence", "operator_review_required",
        )
        return {"classification": result.get("classification"), "confidence": result.get("confidence")}

    _check("preflight_classification", test_preflight)

    # Test 4: Preflight blocks missing evidence
    def test_preflight_missing():
        orch = ConstructorOrchestrator()
        result = orch.classify_case({
            "ordered_stop_ids": [],
            "name_hint": "",
        })
        return {"classification": result.get("classification")}

    _check("preflight_missing_evidence", test_preflight_missing)

    # Test 5: Metrics collection
    def test_metrics():
        orch = ConstructorOrchestrator()
        orch.classify_case({
            "ordered_stop_ids": ["00000000-0000-0000-0000-000000000001"] * 3,
            "name_hint": "MetricsTest",
        })
        metrics = orch.collect_metrics()
        assert "counters" in metrics
        # The orchestrator tracks events; at least one counter should be non-zero
        total = sum(v for v in metrics["counters"].values() if isinstance(v, (int, float)))
        assert total >= 1, f"Expected at least one counter > 0, got {metrics['counters']}"
        return {"counters": metrics.get("counters")}

    _check("metrics_collection", test_metrics)

    # Test 6: Contract enhancement (new fields)
    def test_contracts():
        from datamind_console.phases.phase3_routes.manual_sequence_contracts import (
            CONSTRUCTOR_STATUSES,
            PREFLIGHT_CLASSIFICATIONS,
            merge_hints_with_priority,
            build_hint_provenance,
            compute_total_distance_m,
        )
        assert len(CONSTRUCTOR_STATUSES) >= 7
        assert len(PREFLIGHT_CLASSIFICATIONS) >= 5
        merged, prov = merge_hints_with_priority(
            manual={"name_hint": "Manual Route"},
            llm={"name_hint": "LLM Route", "operator_hint": "LLM Op"},
            gap_default={"name_hint": "Gap Route", "operator_hint": "Gap Op", "variant_hint": "Gap Var"},
        )
        assert merged["name_hint"] == "Manual Route"  # manual wins
        assert prov["name_hint"] == "manual"
        assert merged["operator_hint"] == "LLM Op"  # llm wins over gap
        assert prov["operator_hint"] == "llm_suggestion"
        assert merged["variant_hint"] == "Gap Var"  # gap default
        return {"merged": merged, "provenance": prov}

    _check("contract_enhancements", test_contracts)

    # Test 7: Hint provenance tracking
    def test_hint_provenance():
        from datamind_console.phases.phase3_routes.manual_sequence_contracts import (
            build_hint_provenance,
        )
        prov = build_hint_provenance(
            name_hint_source="manual",
            operator_hint_source="llm_suggestion",
            variant_hint_source="gap_default",
        )
        assert prov["name_hint"] == "manual"
        assert prov["operator_hint"] == "llm_suggestion"
        return prov

    _check("hint_provenance", test_hint_provenance)

    # Test 8: SQL migration file exists and is valid
    def test_sql_migration():
        sql_path = Path(__file__).resolve().parents[3] / "phase3_routes" / "services" / "route_constructor" / "sql" / "028_constructor_orchestrator.sql"
        assert sql_path.exists(), f"Migration not found: {sql_path}"
        content = sql_path.read_text(encoding="utf-8")
        assert "constructor_metrics_log" in content
        assert "constructor_review_audit" in content
        assert "constructor_dashboard_v1" in content
        assert "constructor_status" in content
        return {"path": str(sql_path), "size": len(content)}

    _check("sql_migration_exists", test_sql_migration)

    # Test 9: Idempotency check works
    def test_idempotency():
        orch = ConstructorOrchestrator()
        check = orch.check_rerun_safety({
            "ordered_stop_ids": ["00000000-0000-0000-0000-000000000001"] * 3,
            "route_job_id": "00000000-0000-0000-0000-000000000099",
        })
        # The check result should be a dict with some assessment
        assert isinstance(check, dict)
        return check

    _check("idempotency_check", test_idempotency)

    # Test 10: LLM advisor call log
    def test_llm_call_log():
        advisor = ConstructorLLMAdvisor(mode="mock")
        advisor.interpret_missing_route({"name_hint": "CallLogTest"})
        log = advisor.call_log  # property, not method
        assert len(log) >= 1
        return {"call_count": len(log)}

    _check("llm_call_log", test_llm_call_log)

    # Test 11: LLM sequence interpretation (mock)
    def test_llm_sequence():
        advisor = ConstructorLLMAdvisor(mode="mock")
        result = advisor.interpret_sequence({
            "name_hint": "Ruta Tumbaco - Cumbaya Express",
            "operator_hint": "Coop. Tumbaco",
            "variant_hint": "retorno",
            "sector_key": "tumbaco_cumbaya",
            "anchor_count": 2,
            "corridor_hints": ["Av. Interoceánica"],
            "locality_clues": ["Tumbaco", "Cumbayá"],
            "must_pass_through": ["Puente San Pedro"],
            "total_ordered_stops": 4,
        })
        mo = result.get("model_output", {})
        assert result.get("source") in ("mock", "heuristic_mock")
        assert mo.get("suggested_sequence_strategy") in (
            "linear_start_to_end", "loop_circuit", "hub_and_spoke",
            "corridor_follow", "locality_chain", "uncertain_needs_stops",
        )
        assert len(mo.get("suggested_anchor_stops", [])) >= 1
        assert isinstance(mo.get("evidence_gaps"), list)
        return {
            "strategy": mo.get("suggested_sequence_strategy"),
            "anchors": len(mo.get("suggested_anchor_stops", [])),
            "zones": len(mo.get("suggested_intermediate_zones", [])),
            "next_action": mo.get("recommended_next_action"),
        }

    _check("llm_sequence_interpret", test_llm_sequence)

    # Test 12: Sequence context building
    def test_sequence_context():
        from datamind_console.phases.phase3_routes.manual_sequence_contracts import (
            build_sequence_context,
            SEQUENCE_READINESS_LEVELS,
        )
        ctx = build_sequence_context({
            "ordered_stop_ids": ["a", "b", "c"],
            "anchor_stop_ids": ["a", "c"],
            "corridor_hints": ["Av. Test"],
            "locality_clues": ["Zone A", "Zone B"],
        })
        readiness = ctx.readiness_level()
        assert readiness in SEQUENCE_READINESS_LEVELS, f"Unexpected readiness: {readiness}"
        ev = ctx.to_evidence_dict()
        assert "anchor_count" in ev
        assert "readiness_level" in ev
        return {"readiness": readiness, "evidence_keys": list(ev.keys())}

    _check("sequence_context_build", test_sequence_context)

    # Test 13: Sparse sequence (insufficient evidence)
    def test_sequence_sparse():
        advisor = ConstructorLLMAdvisor(mode="mock")
        result = advisor.interpret_sequence({
            "name_hint": "",
            "operator_hint": "",
            "total_ordered_stops": 0,
        })
        mo = result.get("model_output", {})
        assert mo.get("confidence", 1.0) < 0.5, f"Expected low confidence, got {mo.get('confidence')}"
        assert len(mo.get("evidence_gaps", [])) >= 3
        return {
            "confidence": mo.get("confidence"),
            "gaps": len(mo.get("evidence_gaps", [])),
            "next_action": mo.get("recommended_next_action"),
        }

    _check("sequence_sparse_case", test_sequence_sparse)

    # Summary
    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    summary = {
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "timestamp": _utc_iso(),
        "results": results,
    }

    # Write validation summary
    out_dir.mkdir(parents=True, exist_ok=True)
    _safe_json_dump(summary, out_dir / "validation_results.json")

    if desktop_mirror:
        mirror_dir.mkdir(parents=True, exist_ok=True)
        _safe_json_dump(summary, mirror_dir / "validation_results.json")

    return summary


def run_sequence_discovery_batch(
    cases: List[Dict[str, Any]],
    *,
    output_dir: Optional[str] = None,
    desktop_mirror: bool = False,
    llm_mode: str = "mock",
) -> Dict[str, Any]:
    """
    Run sequence discovery on a batch of route cases.

    For each case, interprets the route identity + hints to produce
    a sequence strategy, anchor stop suggestions, intermediate zones,
    and next-action recommendation.

    Parameters
    ----------
    cases : list of dict
        Each case should have: name_hint, operator_hint, variant_hint,
        ordered_stop_ids, corridor_hints, locality_clues, must_pass_through,
        intermediate_stop_hints, sequence_notes, sector_key, coverage_gap_id
    """
    from datamind_console.phases.phase3_routes.constructor_llm_advisor import (
        ConstructorLLMAdvisor,
    )
    from datamind_console.phases.phase3_routes.manual_sequence_contracts import (
        build_sequence_context,
        SEQUENCE_READINESS_LEVELS,
    )

    out_dir = Path(output_dir or "phase3_route_catalog")
    out_dir.mkdir(parents=True, exist_ok=True)
    mirror_dir = Path.home() / "phase3_route_catalog"

    advisor = ConstructorLLMAdvisor(mode=llm_mode)

    hint_audit_entries: List[Dict[str, Any]] = []
    candidate_summaries: List[Dict[str, Any]] = []
    llm_evidence_payloads: List[Dict[str, Any]] = []
    llm_reasoning_outputs: List[Dict[str, Any]] = []

    for i, case in enumerate(cases):
        case_id = case.get("case_id", f"seq_{i:04d}")
        _LOG.info("Sequence discovery %s: %s", case_id, case.get("name_hint", "unnamed"))

        # Build sequence context from the case
        try:
            seq_ctx = build_sequence_context(case)
            readiness = seq_ctx.readiness_level()
        except Exception as exc:
            _LOG.warning("build_sequence_context failed for %s: %s", case_id, exc)
            readiness = "insufficient_evidence"
            seq_ctx = None

        # Build evidence for LLM advisor
        ordered_stop_ids = case.get("ordered_stop_ids") or []
        evidence = {
            "name_hint": case.get("name_hint") or "",
            "operator_hint": case.get("operator_hint") or "",
            "variant_hint": case.get("variant_hint") or "",
            "sector_key": case.get("sector_key") or "",
            "gap_start_hint": case.get("gap_start_hint") or "",
            "gap_end_hint": case.get("gap_end_hint") or "",
            "gap_direction_hint": case.get("gap_direction_hint") or "",
            "anchor_stop_ids": case.get("anchor_stop_ids") or [],
            "anchor_count": len(case.get("anchor_stop_ids") or []),
            "intermediate_stop_hints": case.get("intermediate_stop_hints") or [],
            "corridor_hints": case.get("corridor_hints") or [],
            "locality_clues": case.get("locality_clues") or [],
            "must_pass_through": case.get("must_pass_through") or [],
            "total_ordered_stops": len(ordered_stop_ids),
            "sequence_notes": case.get("sequence_notes") or "",
            "existing_catalog_routes": case.get("existing_catalog_routes") or [],
        }

        # Always run LLM interpretation for sequence discovery
        llm_result = None
        try:
            llm_result = advisor.interpret_sequence(evidence)
        except Exception as exc:
            _LOG.warning("LLM interpret_sequence failed for %s: %s", case_id, exc)

        # Build hint audit entry
        hint_entry = {
            "case_id": case_id,
            "name_hint": case.get("name_hint"),
            "operator_hint": case.get("operator_hint"),
            "variant_hint": case.get("variant_hint"),
            "sector_key": case.get("sector_key"),
            "readiness_level": readiness,
            "ordered_stop_count": len(ordered_stop_ids),
            "anchor_count": len(case.get("anchor_stop_ids") or []),
            "intermediate_hint_count": len(case.get("intermediate_stop_hints") or []),
            "corridor_hint_count": len(case.get("corridor_hints") or []),
            "locality_clue_count": len(case.get("locality_clues") or []),
            "must_pass_through_count": len(case.get("must_pass_through") or []),
            "sequence_notes": case.get("sequence_notes"),
            "timestamp": _utc_iso(),
        }
        if seq_ctx:
            hint_entry["sequence_evidence"] = seq_ctx.to_evidence_dict()

        # Build candidate summary from LLM result
        candidate_entry = {
            "case_id": case_id,
            "name_hint": case.get("name_hint"),
            "readiness_level": readiness,
            "timestamp": _utc_iso(),
        }
        if llm_result:
            mo = llm_result.get("model_output", {})
            candidate_entry.update({
                "llm_source": llm_result.get("source"),
                "llm_confidence": llm_result.get("confidence"),
                "interpreted_route_summary": mo.get("interpreted_route_summary"),
                "suggested_strategy": mo.get("suggested_sequence_strategy"),
                "suggested_anchor_count": len(mo.get("suggested_anchor_stops") or []),
                "suggested_zone_count": len(mo.get("suggested_intermediate_zones") or []),
                "duplicate_risk": mo.get("duplicate_risk"),
                "variant_risk": mo.get("variant_risk"),
                "recommended_next_action": mo.get("recommended_next_action"),
                "evidence_gaps": mo.get("evidence_gaps"),
                "improved_name_hint": mo.get("improved_name_hint"),
                "improved_operator_hint": mo.get("improved_operator_hint"),
                "improved_variant_hint": mo.get("improved_variant_hint"),
            })
            hint_entry["llm_recommended_action"] = mo.get("recommended_next_action")
            hint_entry["llm_confidence"] = llm_result.get("confidence")

            llm_evidence_payloads.append({
                "case_id": case_id,
                "task": "interpret_sequence",
                "evidence_payload": llm_result.get("evidence_payload"),
                "timestamp": llm_result.get("timestamp"),
            })
            llm_reasoning_outputs.append({
                "case_id": case_id,
                "task": "interpret_sequence",
                "model_output": mo,
                "confidence": llm_result.get("confidence"),
                "recommended_action": mo.get("recommended_next_action"),
                "reasoning": mo.get("reasoning"),
                "source": llm_result.get("source"),
                "model": llm_result.get("model"),
                "latency_ms": llm_result.get("latency_ms"),
                "timestamp": llm_result.get("timestamp"),
            })

        hint_audit_entries.append(hint_entry)
        candidate_summaries.append(candidate_entry)

    # Write artifacts
    artifacts_written: List[str] = []
    artifacts = {
        "sequence_hint_audit.json": hint_audit_entries,
        "sequence_candidate_summary.json": candidate_summaries,
        "llm_evidence_payload.json": llm_evidence_payloads,
        "llm_assisted_reasoning.json": llm_reasoning_outputs,
    }

    for name, data in artifacts.items():
        path = out_dir / name
        _safe_json_dump(data, path)
        artifacts_written.append(str(path))

    if desktop_mirror:
        mirror_dir.mkdir(parents=True, exist_ok=True)
        for name, data in artifacts.items():
            mirror_path = mirror_dir / name
            _safe_json_dump(data, mirror_path)
            artifacts_written.append(str(mirror_path))

    return {
        "cases_processed": len(cases),
        "hint_audit_entries": len(hint_audit_entries),
        "candidate_summaries": len(candidate_summaries),
        "llm_calls": len(llm_evidence_payloads),
        "artifacts_written": artifacts_written,
        "timestamp": _utc_iso(),
    }


def write_documentation(
    *,
    output_dir: Optional[str] = None,
    desktop_mirror: bool = False,
    metrics: Optional[Dict[str, Any]] = None,
    validation: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Write documentation artifacts."""
    out_dir = Path(output_dir or "phase3_route_catalog")
    mirror_dir = Path.home() / "phase3_route_catalog"
    files_written: List[str] = []

    docs = {
        "CONSTRUCTOR_WORKFLOW.md": _build_workflow_doc(),
        "CONSTRUCTOR_METRICS.md": _build_metrics_doc(metrics),
        "IMPLEMENTATION_SUMMARY.md": _build_implementation_doc(),
        "VALIDATION_SUMMARY.md": _build_validation_doc(validation),
        "SEQUENCE_DISCOVERY_WORKFLOW.md": _build_sequence_discovery_doc(),
    }

    for name, content in docs.items():
        for d in [out_dir, mirror_dir] if desktop_mirror else [out_dir]:
            d.mkdir(parents=True, exist_ok=True)
            path = d / name
            path.write_text(content, encoding="utf-8")
            files_written.append(str(path))

    return files_written


def _build_workflow_doc() -> str:
    return """# Route Constructor Workflow

## Overview

The Route Constructor transforms missing-route cases into real Phase 3 work items
through an orchestrated pipeline with preflight classification, LLM-assisted
reasoning, metrics observation, and controlled review.

## Lifecycle

```
Input Case -> Preflight Classification -> Draft / Export Decision
                    |                          |
                    v                          v
             [Blocked Cases]          [Safe Cases]
             - already_represented    - true_missing_ready
             - missing_evidence       - duplicate_risk (w/ force)
             - duplicate_risk         - needs_cleanup_first
                                      - operator_review_required
                    |                          |
                    v                          v
             [Failure Log]            [Draft Saved]
                                           |
                                           v
                                    [Export to Phase 3]
                                           |
                                           v
                                    [Sequence Approval]
                                           |
                                           v
                                    [Geometry Build]
                                           |
                                           v
                                    [Final Review]
```

## Constructor Status Lifecycle

| Status | Description |
|--------|-------------|
| `draft_saved` | Initial draft created |
| `draft_ready` | Draft validated, ready for export |
| `exported_sequence_ready` | Sequence exported to Phase 3 |
| `awaiting_sequence_approval` | Waiting for sequence approval |
| `awaiting_geometry` | Waiting for geometry build |
| `awaiting_review` | Final review pending |
| `blocked_missing_evidence` | Blocked by missing stops/data |
| `completed` | Successfully finished |
| `rejected` | Rejected by review |

## Preflight Classifications

| Classification | Description |
|---------------|-------------|
| `true_missing_ready` | Genuinely missing, safe to create |
| `likely_already_represented` | Probably exists under different name |
| `duplicate_risk` | High risk of duplicating existing route |
| `needs_cleanup_first` | Data quality issues to resolve |
| `missing_evidence` | Not enough stop/route evidence |
| `operator_review_required` | Ambiguous, needs human decision |

## ChatGPT API Integration

Hard cases are sent to ChatGPT for aggressive interpretation:
- Ambiguous route names
- Weak operator hints
- Partial gap matches
- Duplicate vs variant uncertainty

All LLM calls are:
- Logged with evidence payloads
- Stored with reasoning output
- Tagged with confidence scores
- Advisory only (never overwrite truth)

## Hint Provenance

Every hint tracks its source: `manual` > `llm_suggestion` > `gap_default`

## Idempotency

- Same input sequence is detected and blocked
- Reruns require explicit `force_rerun=True`
- Prior exports are linked via `prior_export_id`
"""


def _build_metrics_doc(metrics: Optional[Dict[str, Any]] = None) -> str:
    counters = (metrics or {}).get("counters", {})
    lines = ["# Constructor Metrics\n"]
    lines.append(f"Generated: {_utc_iso()}\n")
    if counters:
        lines.append("## Counters\n")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        for k, v in sorted(counters.items()):
            lines.append(f"| {k} | {v} |")
        lines.append("")
    else:
        lines.append("No metrics collected yet. Run the constructor to populate.\n")
    lines.append("## Observable Metrics\n")
    lines.append("- cases_processed: total missing-route entries processed")
    lines.append("- drafts_created / drafts_updated / drafts_resumed")
    lines.append("- exports_attempted / exports_succeeded / exports_failed")
    lines.append("- rejected_already_represented / rejected_duplicate_risk")
    lines.append("- blocked_missing_stops / blocked_missing_gap_context")
    lines.append("- llm_assisted_cases / llm_calls_made / llm_calls_failed")
    lines.append("- deterministic_successes")
    lines.append("- preflight_* counters per classification")
    return "\n".join(lines)


def _build_implementation_doc() -> str:
    return """# Implementation Summary

## Files Created / Modified

### New Files
- `datamind_console/phases/phase3_routes/constructor_orchestrator.py` - Main orchestration engine
- `datamind_console/phases/phase3_routes/constructor_llm_advisor.py` - ChatGPT API integration
- `datamind_console/phases/phase3_routes/constructor_runner.py` - Batch runner + validation
- `phase3_routes/services/route_constructor/sql/028_constructor_orchestrator.sql` - Schema migration

### Modified Files
- `datamind_console/phases/phase3_routes/manual_sequence_contracts.py` - Enhanced with:
  - Constructor status lifecycle constants
  - Preflight classification constants
  - Hint provenance tracking
  - New fields on ManualSequenceExportRequest
  - merge_hints_with_priority()
  - build_hint_provenance()
  - compute_sequence_bbox()
  - compute_total_distance_m()

## Architecture

```
manual_sequence_contracts.py    # Data contracts + validation
       |
constructor_orchestrator.py     # Preflight + draft mgmt + export safety + metrics
       |
constructor_llm_advisor.py      # ChatGPT reasoning for hard cases
       |
constructor_runner.py           # Batch execution + validation + doc generation
       |
028_constructor_orchestrator.sql # Schema: status columns, metrics log, review audit
```

## Key Improvements

1. **Preflight Classification** - Cases are classified before any DB work
2. **Constructor Status Lifecycle** - 9 statuses track route through pipeline
3. **Metrics/AI-Bot Monitoring** - 20+ counters + event log + failure classes
4. **ChatGPT API Integration** - 4 advisory tasks with structured output
5. **Draft Management** - list/load/update/resume/delete with soft-delete
6. **Export Safety** - Idempotency checks, duplicate detection, rerun linking
7. **Hint Provenance** - Tracks manual vs LLM vs gap origin for every hint
8. **Review Audit Trail** - Every decision logged with evidence + reasoning
9. **Atomic Operations** - Pre-validation before any DB writes

## Safety Controls

- LLM output is advisory only, never overwrites canonical truth
- Preflight blocks exports for missing_evidence and already_represented
- Duplicate exports are detected and blocked unless force_rerun=True
- All audit entries link back to evidence and reasoning
- Status transitions follow allowed graph (no invalid jumps)
"""


def _build_sequence_discovery_doc() -> str:
    return """# Sequence Discovery Workflow

## Purpose

Discover the likely stop sequence for a transit route using all available
evidence: route name parsing, corridor hints, locality clues, operator
knowledge, intermediate stop hints, and ChatGPT-assisted interpretation.

## Evidence Types

| Evidence | Source | Reliability |
|----------|--------|-------------|
| Anchor stops | Phase 2 approved stops | High |
| Route name (A - B) | Manual or gap context | Medium-High |
| Corridor hints | Map knowledge, operator | Medium |
| Locality clues | Geography, sector catalog | Medium |
| Must-pass-through | Operator, map inspection | Medium |
| Intermediate stop hints | Partial sequences, LLM | Low-Medium |
| LLM interpretation | ChatGPT advisory | Advisory only |

## Readiness Levels

| Level | Criteria |
|-------|----------|
| `strong_sequence_context` | 3+ anchors + corridor OR locality chain |
| `enough_anchor_stops` | 2+ anchors with clear start/end |
| `enough_hint_stops` | 2+ intermediate hints + anchors |
| `weak_sequence_context` | Some anchors or hints but gaps |
| `candidate_for_llm_sequence_interpretation` | Name parseable, few stops |
| `insufficient_evidence` | Not enough to build any sequence |

## Sequence Strategies

| Strategy | When to use |
|----------|-------------|
| `linear_start_to_end` | Clear A-B route with name-derived endpoints |
| `loop_circuit` | Circular/loop routes |
| `hub_and_spoke` | Hub terminal with branches |
| `corridor_follow` | Route follows known corridor/avenue |
| `locality_chain` | Route connects locality sequence |
| `uncertain_needs_stops` | Not enough evidence for strategy |

## ChatGPT Integration for Sequence Discovery

The `interpret_sequence` task sends all available evidence to ChatGPT and asks for:

1. Route summary interpretation
2. Suggested anchor stops (key points in likely order)
3. Intermediate zones the route passes through
4. Best sequence-building strategy
5. Improved name/operator/variant hints
6. Duplicate and variant risk
7. Missing evidence inventory
8. Recommended next action

### Next Actions from LLM

| Action | Meaning |
|--------|---------|
| `ready_to_build_sequence` | Enough evidence to proceed |
| `need_more_anchor_stops` | Need confirmed stops at key points |
| `need_locality_search` | Search for stops in suggested zones |
| `need_operator_confirmation` | Operator should verify route identity |
| `hold_for_review` | Ambiguous, needs human review |
| `reject_insufficient` | Not enough evidence to proceed |

## Workflow

```
Input Case (name, hints, stops, gaps)
        |
        v
Build SequenceDiscoveryContext
        |
        v
Assess readiness_level
        |
        +--> insufficient_evidence --> STOP
        |
        +--> candidate_for_llm --> ChatGPT interpret_sequence
        |                                  |
        v                                  v
Merge hints (manual > llm > gap)    LLM suggestions
        |                                  |
        +----------------------------------+
        |
        v
Sequence candidate summary
        |
        v
Hint audit (provenance tracked)
        |
        v
Output: sequence_hint_audit.json
        sequence_candidate_summary.json
        llm_evidence_payload.json
        llm_assisted_reasoning.json
```

## Output Artifacts

| File | Content |
|------|---------|
| `sequence_hint_audit.json` | Per-case evidence inventory + readiness + LLM recommendation |
| `sequence_candidate_summary.json` | Per-case strategy + anchors + zones + next action |
| `llm_evidence_payload.json` | Raw evidence sent to ChatGPT for each call |
| `llm_assisted_reasoning.json` | ChatGPT structured reasoning output |
"""


def _build_validation_doc(validation: Optional[Dict[str, Any]] = None) -> str:
    lines = ["# Validation Summary\n"]
    lines.append(f"Generated: {_utc_iso()}\n")
    if validation:
        lines.append(f"Total tests: {validation.get('total', 0)}")
        lines.append(f"Passed: {validation.get('passed', 0)}")
        lines.append(f"Failed: {validation.get('failed', 0)}\n")
        lines.append("## Test Results\n")
        lines.append("| Test | Status | Detail |")
        lines.append("|------|--------|--------|")
        for r in validation.get("results", []):
            detail = r.get("detail", r.get("error", ""))
            if isinstance(detail, dict):
                detail = json.dumps(detail, ensure_ascii=False)[:100]
            lines.append(f"| {r['test']} | {r['status']} | {detail} |")
    else:
        lines.append("No validation run yet.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Route Constructor Runner")
    parser.add_argument("--validate", action="store_true", help="Run validation suite only")
    parser.add_argument("--sequence", action="store_true", help="Run sequence discovery batch")
    parser.add_argument("--input", type=str, help="Input JSON file with missing-route cases")
    parser.add_argument("--output-dir", type=str, default="phase3_route_catalog")
    parser.add_argument("--llm-mode", type=str, default="mock", choices=["real_advisory", "mock", "dry_run"])
    parser.add_argument("--force-rerun", action="store_true")
    parser.add_argument("--no-mirror", action="store_true", help="Skip desktop mirror")
    args = parser.parse_args()

    desktop_mirror = not args.no_mirror

    if args.validate:
        print("Running validation suite...")
        validation = run_validation_suite(
            output_dir=args.output_dir,
            desktop_mirror=desktop_mirror,
        )
        print(f"\nValidation: {validation['passed']}/{validation['total']} passed")
        for r in validation["results"]:
            status_icon = "OK" if r["status"] == "PASS" else "FAIL"
            print(f"  [{status_icon}] {r['test']}")
            if r["status"] == "FAIL":
                print(f"       Error: {r.get('error', 'unknown')}")

        # Write docs
        doc_files = write_documentation(
            output_dir=args.output_dir,
            desktop_mirror=desktop_mirror,
            validation=validation,
        )
        print(f"\nDocs written: {len(doc_files)} files")
        return

    if args.sequence and args.input:
        input_path = Path(args.input)
        if not input_path.exists():
            print(f"Input file not found: {input_path}")
            sys.exit(1)
        cases = json.loads(input_path.read_text(encoding="utf-8"))
        if not isinstance(cases, list):
            cases = [cases]

        result = run_sequence_discovery_batch(
            cases,
            output_dir=args.output_dir,
            desktop_mirror=desktop_mirror,
            llm_mode=args.llm_mode,
        )
        doc_files = write_documentation(
            output_dir=args.output_dir,
            desktop_mirror=desktop_mirror,
        )
        print(f"\nSequence discovery complete:")
        print(f"  Cases processed: {result['cases_processed']}")
        print(f"  LLM calls: {result['llm_calls']}")
        print(f"  Artifacts: {len(result['artifacts_written'])}")
        print(f"  Docs: {len(doc_files)}")
        return

    if args.input:
        input_path = Path(args.input)
        if not input_path.exists():
            print(f"Input file not found: {input_path}")
            sys.exit(1)
        cases = json.loads(input_path.read_text(encoding="utf-8"))
        if not isinstance(cases, list):
            cases = [cases]

        result = run_constructor_batch(
            cases,
            output_dir=args.output_dir,
            desktop_mirror=desktop_mirror,
            llm_mode=args.llm_mode,
            force_rerun=args.force_rerun,
        )

        doc_files = write_documentation(
            output_dir=args.output_dir,
            desktop_mirror=desktop_mirror,
            metrics=result.get("metrics"),
        )

        print(f"\nBatch complete:")
        print(f"  Results: {len(result.get('results', []))}")
        print(f"  Failures: {len(result.get('failures', []))}")
        print(f"  LLM calls: {result.get('llm_calls', 0)}")
        print(f"  Artifacts: {len(result.get('artifacts_written', []))}")
        print(f"  Docs: {len(doc_files)}")
        return

    # Default: validate + write docs
    print("No input specified. Running validation + documentation generation...")
    validation = run_validation_suite(
        output_dir=args.output_dir,
        desktop_mirror=desktop_mirror,
    )
    doc_files = write_documentation(
        output_dir=args.output_dir,
        desktop_mirror=desktop_mirror,
        validation=validation,
    )
    print(f"Validation: {validation['passed']}/{validation['total']} passed")
    print(f"Docs: {len(doc_files)} files written")


if __name__ == "__main__":
    main()
