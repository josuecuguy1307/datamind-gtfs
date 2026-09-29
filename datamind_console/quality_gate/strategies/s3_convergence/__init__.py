"""S3 Two-Pass Convergence strategy for the HADES Quality Gate.

Applies all proposed fixes speculatively to a working copy, then re-runs
the gate. Commits only when the system reaches a fixed point (no new issues,
no undone fixes). Reverts everything on divergence or max iterations.
"""
from .gate import S3ConvergenceGate

__all__ = ["S3ConvergenceGate"]
