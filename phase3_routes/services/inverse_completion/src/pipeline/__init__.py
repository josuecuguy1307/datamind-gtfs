"""DEPRECATED — see pipeline.phase_4_5.pair_detection.inverse_completion."""
import warnings as _warnings
_warnings.warn(
    "phase3_routes.services.inverse_completion.src.pipeline is deprecated; "
    "import from pipeline.phase_4_5.pair_detection.inverse_completion.src.pipeline instead.",
    DeprecationWarning,
    stacklevel=2,
)
from pipeline.phase_4_5.pair_detection.inverse_completion.src.pipeline import *  # noqa: E402,F401,F403
from pipeline.phase_4_5.pair_detection.inverse_completion.src.pipeline.step15_inverse_completion import (  # noqa: E402,F401
    analyze_inverse_completion,
    analyze_inverse_proposals,
    dispatch_targeted_inverse_search_for_slot,
    get_persisted_direction_readiness,
    get_inverse_completion_summary,
    get_step20_direction_gate,
    get_direction_readiness,
)
