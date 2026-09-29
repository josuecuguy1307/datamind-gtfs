from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, str(default))).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return bool(default)


@dataclass(frozen=True)
class AutopilotFeatureFlags:
    live_bridge_enabled: bool = True
    db_persistence_enabled: bool = False
    auto_resume_enabled: bool = True
    policy_profiles_enabled: bool = True
    worker_enabled: bool = False
    slo_alerting_enabled: bool = True
    patch_chaining_enabled: bool = True
    patch_auto_dispatch_enabled: bool = False
    adaptive_retry_enabled: bool = True
    shadow_learned_retry_ranking_enabled: bool = False

    @staticmethod
    def from_env() -> "AutopilotFeatureFlags":
        return AutopilotFeatureFlags(
            live_bridge_enabled=_env_bool("HADES_AUTOPILOT_FF_LIVE_BRIDGE", True),
            db_persistence_enabled=_env_bool("HADES_AUTOPILOT_FF_DB_PERSISTENCE", False),
            auto_resume_enabled=_env_bool("HADES_AUTOPILOT_FF_AUTO_RESUME", True),
            policy_profiles_enabled=_env_bool("HADES_AUTOPILOT_FF_POLICY_PROFILES", True),
            worker_enabled=_env_bool("HADES_AUTOPILOT_FF_WORKER", False),
            slo_alerting_enabled=_env_bool("HADES_AUTOPILOT_FF_SLO_ALERTING", True),
            patch_chaining_enabled=_env_bool("HADES_AUTOPILOT_FF_PATCH_CHAINING", True),
            patch_auto_dispatch_enabled=_env_bool("HADES_AUTOPILOT_FF_PATCH_AUTO_DISPATCH", False),
            adaptive_retry_enabled=_env_bool("HADES_AUTOPILOT_FF_ADAPTIVE_RETRY", True),
            shadow_learned_retry_ranking_enabled=_env_bool("HADES_AUTOPILOT_FF_SHADOW_LEARNED_RETRY_RANKING", False),
        )
