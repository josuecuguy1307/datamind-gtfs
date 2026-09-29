"""DEPRECATED — see pipeline.phase_4_5.pair_detection.inverse_completion."""
import warnings as _warnings
_warnings.warn(
    "phase3_routes.services.inverse_completion.src.dispatch is deprecated; "
    "import from pipeline.phase_4_5.pair_detection.inverse_completion.src.dispatch instead.",
    DeprecationWarning,
    stacklevel=2,
)
from pipeline.phase_4_5.pair_detection.inverse_completion.src.dispatch.targeted_inverse_dispatch import *  # noqa: E402,F401,F403
