"""Convergence and divergence detection for S3 Two-Pass strategy.

A canton converges when a re-run of the gate after applying fixes produces
no new issues and doesn't want to undo any prior fix. Divergence is detected
when fixes oscillate across passes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Set


@dataclass
class PassResult:
    """Statistics for a single gate pass within the convergence loop."""
    pass_num: int
    issues_found: int
    fixes_proposed: int
    fixes_applied: int
    fixes_deferred: int
    new_issues_vs_previous: int      # issues in this pass not seen in prior pass
    resolved_from_previous: int      # issues from prior pass no longer present
    undone_fixes: int                # fixes this pass wants to reverse from prior
    issue_keys: Set[str] = field(default_factory=set)  # for diffing across passes


def make_issue_key(entity_type: str, entity_id: str, rule_name: str) -> str:
    """Canonical key for an issue, used to diff across passes."""
    return f"{entity_type}::{entity_id}::{rule_name}"


def compute_pass_diff(
    current_issue_keys: Set[str],
    previous_issue_keys: Set[str],
) -> tuple[int, int]:
    """Return (new_issues, resolved_issues) compared to the previous pass."""
    new = len(current_issue_keys - previous_issue_keys)
    resolved = len(previous_issue_keys - current_issue_keys)
    return new, resolved


def is_converged(passes: List[PassResult]) -> bool:
    """Converged when the latest pass introduced no new issues.

    Requires at least 2 passes — a single pass cannot demonstrate stability.
    Undone fixes (re-applications of the same fix) are allowed as long as no
    NEW issues appeared — they indicate idempotent re-fixing, not instability.
    """
    if len(passes) < 2:
        return False
    latest = passes[-1]
    return latest.new_issues_vs_previous == 0


def is_diverged(passes: List[PassResult]) -> bool:
    """Diverged: the last 3 passes show a pattern of accumulating issues
    without improvement. Specifically: the 2 most recent passes each
    introduced new issues, AND the final pass did not decrease total
    issue count. Pass 0 of the window is ignored because
    new_issues_vs_previous is structurally 0 on the first observation.
    """
    if len(passes) < 3:
        return False
    last_three = passes[-3:]
    all_introduced_new = all(
        p.new_issues_vs_previous > 0 for p in last_three[1:]
    )
    not_decreasing = last_three[-1].issues_found >= last_three[-2].issues_found
    return all_introduced_new and not_decreasing


def convergence_summary(passes: List[PassResult]) -> dict:
    """Human-readable summary of the convergence trace for diagnostics."""
    return {
        "num_passes": len(passes),
        "converged": is_converged(passes),
        "diverged": is_diverged(passes),
        "issues_per_pass": [p.issues_found for p in passes],
        "new_per_pass": [p.new_issues_vs_previous for p in passes],
        "resolved_per_pass": [p.resolved_from_previous for p in passes],
        "undone_per_pass": [p.undone_fixes for p in passes],
        "fixes_applied_per_pass": [p.fixes_applied for p in passes],
    }
