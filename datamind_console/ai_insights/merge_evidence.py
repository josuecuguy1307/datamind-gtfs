"""DEPRECATED — moved to ``pipeline.phase_4_5.pair_detection.merge_evidence``.

Shim retained for one release cycle. See
``workspace/skills/direction_construction.md``.
"""

from __future__ import annotations

import warnings as _warnings

_warnings.warn(
    "datamind_console.ai_insights.merge_evidence is deprecated; "
    "import from pipeline.phase_4_5.pair_detection.merge_evidence instead.",
    DeprecationWarning,
    stacklevel=2,
)

from pipeline.phase_4_5.pair_detection.merge_evidence import *  # noqa: E402,F401,F403
from pipeline.phase_4_5.pair_detection.merge_evidence import (  # noqa: E402,F401
    RoutePairEvidenceExtractor,
)
