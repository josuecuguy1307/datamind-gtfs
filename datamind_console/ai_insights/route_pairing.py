"""DEPRECATED — moved to ``pipeline.phase_4_5.pair_detection.route_pairing``.

This shim re-exports from the new location for one release cycle. New code
should import from ``pipeline.phase_4_5.pair_detection.route_pairing``
directly. See ``workspace/skills/direction_construction.md``.
"""

from __future__ import annotations

import warnings as _warnings

_warnings.warn(
    "datamind_console.ai_insights.route_pairing is deprecated; "
    "import from pipeline.phase_4_5.pair_detection.route_pairing instead.",
    DeprecationWarning,
    stacklevel=2,
)

from pipeline.phase_4_5.pair_detection.route_pairing import *  # noqa: E402,F401,F403
from pipeline.phase_4_5.pair_detection.route_pairing import (  # noqa: E402,F401
    LonLat,
    haversine_m,
    clip01,
)
