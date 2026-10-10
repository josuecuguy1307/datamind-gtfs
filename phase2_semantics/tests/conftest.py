"""Make ``src`` (phase2_semantics/src) importable when pytest runs from the repo root."""
import sys
from pathlib import Path

_PHASE2 = str(Path(__file__).resolve().parents[1])
if _PHASE2 not in sys.path:
    sys.path.insert(0, _PHASE2)
