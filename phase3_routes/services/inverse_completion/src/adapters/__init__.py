"""DEPRECATED — see pipeline.phase_4_5.pair_detection.inverse_completion."""
import warnings as _warnings
_warnings.warn(
    "phase3_routes.services.inverse_completion.src.adapters is deprecated; "
    "import from pipeline.phase_4_5.pair_detection.inverse_completion.src.adapters instead.",
    DeprecationWarning,
    stacklevel=2,
)
from pipeline.phase_4_5.pair_detection.inverse_completion.src.adapters.merge_proposal_adapter import (  # noqa: E402,F401
    analyze_inverse_proposals_for_results,
)
