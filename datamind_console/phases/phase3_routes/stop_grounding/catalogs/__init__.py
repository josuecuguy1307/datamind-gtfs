"""Stop grounding configuration catalog loader."""
import json
import os
from pathlib import Path
from typing import Any, Dict

_CATALOG_DIR = Path(__file__).parent
_CONFIG_PATH = _CATALOG_DIR / "stop_grounding_config.json"
_cached_config: Dict[str, Any] | None = None


def load_stop_grounding_config() -> Dict[str, Any]:
    """Load the stop grounding configuration catalog. Cached after first load."""
    global _cached_config
    if _cached_config is not None:
        return _cached_config

    # Allow override via environment variable
    config_path = os.getenv("STOP_GROUNDING_CONFIG_PATH", str(_CONFIG_PATH))
    with open(config_path, encoding="utf-8") as f:
        _cached_config = json.load(f)
    return _cached_config


def get_config_section(section: str) -> Dict[str, Any]:
    """Get a specific section of the config."""
    return load_stop_grounding_config().get(section, {})
