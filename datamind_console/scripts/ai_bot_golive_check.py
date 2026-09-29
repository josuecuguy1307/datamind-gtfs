from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _db_probe() -> Dict[str, Any]:
    try:
        from datamind_console.db.db import db_conn

        with db_conn(readonly=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      current_database() AS db,
                      COALESCE(inet_server_addr()::text, 'local_socket') AS host,
                      inet_server_port() AS port
                    """
                )
                row = cur.fetchone() or ()
        if isinstance(row, tuple):
            db, host, port = row[0], row[1], row[2]
        else:
            db, host, port = row.get('db'), row.get('host'), row.get('port')
        return {'ok': True, 'db': db, 'host': host, 'port': port}
    except Exception as e:
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}


def _stage_coverage(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in rows:
        if str(r.get('phase') or '') != 'phase3' or str(r.get('event_type') or '') != 'run':
            continue
        stage = str(r.get('stage') or '').strip()
        if not stage:
            continue
        out[stage] = int(out.get(stage, 0)) + 1
    return out


def _build_operator_label_soft_gate(*, operator_label_count_30d: int, operator_label_min: int) -> Dict[str, Any]:
    threshold = int(max(0, operator_label_min))
    count = int(max(0, operator_label_count_30d))
    return {
        'enabled': True,
        'min_operator_labels_30d': threshold,
        'operator_label_count_30d': count,
        'pass': count >= threshold,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='AI Bot go-live quick readiness check (2-min).')
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--strict', action='store_true', help='Exit non-zero if hard readiness checks fail.')
    parser.add_argument('--json', action='store_true', help='Print JSON only.')
    parser.add_argument(
        '--operator-label-min',
        type=int,
        default=5,
        help='Minimum operator label rows expected in last 30 days (soft gate only).',
    )
    args = parser.parse_args()

    from datamind_console.ai_insights.service import AIInsightsService

    svc = AIInsightsService()
    db = _db_probe()
    # Prime storage diagnostics so mode reflects actual runtime path in this process.
    _ = svc.run_logs(limit=50)
    _ = svc.model_metrics(limit=20)
    _ = svc.train_events(limit=20)
    storage = svc.storage_status()
    coverage = svc.telemetry_coverage(days=int(args.days))
    sanity = svc.telemetry_sanity_checks(days=int(args.days))
    cmp1 = svc.compare_latest_vs_recent(phase='phase1', lookback=20)
    cmp3 = svc.compare_latest_vs_recent(phase='phase3', lookback=20)
    readiness = svc.model_readiness()
    fb = svc.operator_feedback_summary(days=max(30, int(args.days)))
    fb_rows = svc.operator_feedback(days=max(30, int(args.days)), limit=50000)
    label_metrics = svc.operator_label_metrics(days=max(30, int(args.days)))

    run_rows = svc.run_logs(limit=200000)
    stage_cov = _stage_coverage(run_rows)

    p1_runs = int((coverage.get('phase_run_counts') or {}).get('phase1') or 0)
    p3_runs = int((coverage.get('phase_run_counts') or {}).get('phase3') or 0)
    mode = str(storage.get('mode') or '')

    required_p3_stages = ['step_20_sequences', 'step_30_geometry', 'step_35_rank', 'step_40_approve']
    missing_stages = [s for s in required_p3_stages if int(stage_cov.get(s) or 0) <= 0]

    hard_checks = {
        'db_connected': bool(db.get('ok')),
        'storage_db_mode': (mode == 'db'),
        'phase1_run_logs_present': p1_runs > 0,
        'phase3_run_logs_present': p3_runs > 0,
        'phase3_compare_available': bool(cmp3.get('ok')),
        'phase3_step_coverage_present': len(missing_stages) == 0,
    }

    # Known soft gap: still mostly heuristic labels for phase3 until operator labels scale.
    phase3_ready = dict(readiness.get('phase3_sequence_risk') or {})
    feedback_rows = int(fb.get('total_feedback_rows') or 0)
    phase3_fb_rows = [r for r in fb_rows if str(r.get('target_phase') or '') == 'phase3']
    phase3_feedback_rows = int(len(phase3_fb_rows))
    operator_label_count_30d = int(label_metrics.get('operator_label_count_30d') or 0)
    phase3_warning_label_rows = int(
        sum(1 for r in phase3_fb_rows if str(r.get('sequence_warning_correct') or '').strip())
    )
    phase3_reorder_action_rows = int(
        sum(1 for r in phase3_fb_rows if str(r.get('reorder_action_taken') or '').strip())
    )

    operator_label_soft_gate = _build_operator_label_soft_gate(
        operator_label_count_30d=operator_label_count_30d,
        operator_label_min=int(args.operator_label_min),
    )

    soft_warnings: List[str] = []
    if not bool(operator_label_soft_gate.get('pass')):
        soft_warnings.append(
            'Operator label capture soft gate not met; '
            f"operator_label_count_30d={operator_label_count_30d} < min={operator_label_soft_gate.get('min_operator_labels_30d')}"
        )
    if str(phase3_ready.get('reason') or '') in {'not_enough_logged_runs', 'not_enough_labeled_rows', 'class_imbalance_high'}:
        soft_warnings.append(f"Phase3 ML readiness is limited: {phase3_ready.get('reason')}")
    if int(sanity.get('issue_count') or 0) > 0:
        soft_warnings.append(f"Telemetry sanity has {int(sanity.get('issue_count') or 0)} issue(s); review before go-live.")

    hard_failed = [k for k, v in hard_checks.items() if not bool(v)]
    status = 'ready' if not hard_failed and not soft_warnings else ('ready_with_warnings' if not hard_failed else 'not_ready')

    out = {
        'status': status,
        'hard_checks': hard_checks,
        'hard_failed': hard_failed,
        'soft_warnings': soft_warnings,
        'db_probe': db,
        'storage_mode': mode,
        'storage_mode_detail': storage.get('mode_detail'),
        'coverage': coverage,
        'phase3_stage_coverage': stage_cov,
        'phase3_missing_required_stages': missing_stages,
        'compare_phase1_ok': bool(cmp1.get('ok')),
        'compare_phase3_ok': bool(cmp3.get('ok')),
        'sanity_issue_count': int(sanity.get('issue_count') or 0),
        'soft_gate_checks': {
            'operator_label_capture_required': operator_label_soft_gate,
        },
        'operator_label_stats': {
            'total_feedback_rows': feedback_rows,
            'phase3_feedback_rows': phase3_feedback_rows,
            'phase3_sequence_warning_correct_rows': phase3_warning_label_rows,
            'phase3_reorder_action_rows': phase3_reorder_action_rows,
            'operator_label_count_30d': operator_label_count_30d,
            'operator_good_rate_30d': label_metrics.get('operator_good_rate_30d'),
            'sequence_warning_correct_rate': label_metrics.get('sequence_warning_correct_rate'),
            'reorder_helpful_rate': label_metrics.get('reorder_helpful_rate'),
        },
        'readiness_phase3': phase3_ready,
        'feedback_summary': fb,
    }

    if args.json:
        print(json.dumps(out, ensure_ascii=True, indent=2))
    else:
        print('AI_BOT_GOLIVE', json.dumps(out, ensure_ascii=True))

    if args.strict and hard_failed:
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
