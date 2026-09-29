"""S4 Hybrid strategy — Threshold + Shadow Cross-Validator + Convergence.

Self-contained: does NOT import from s1_thresholds/, s2_shadow/, or s3_convergence/.
"""

from .gate import S4HybridGate

__all__ = ["S4HybridGate"]
