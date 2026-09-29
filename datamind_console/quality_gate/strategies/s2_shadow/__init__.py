"""S2 Shadow + Cross-Validator strategy for the HADES Quality Gate.

For each proposed fix, apply it to an in-memory shadow copy, then run an
independent cross-validator.  Commit only if the cross-validator passes.
"""

from .gate import S2ShadowGate

__all__ = ["S2ShadowGate"]
