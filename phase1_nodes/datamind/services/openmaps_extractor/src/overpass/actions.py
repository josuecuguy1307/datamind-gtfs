from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Tuple

JsonDict = Dict[str, Any]


# ---------------------------------------------------------
# Data model
# ---------------------------------------------------------

@dataclass(frozen=True)
class ActionSpec:
    id: str
    template_path: Path
    default_params: JsonDict
    outputs: list[str]


# ---------------------------------------------------------
# Load & normalize actions.json
# ---------------------------------------------------------

def load_actions(actions_path: str) -> Dict[str, JsonDict]:
    """
    Loads actions.json and returns:
      { action_id -> raw action dict }
    """
    path = Path(actions_path)
    data = json.loads(path.read_text(encoding="utf-8"))

    if not isinstance(data, dict) or "actions" not in data:
        raise ValueError("actions.json must be a dict with key 'actions'")

    actions = data["actions"]
    if not isinstance(actions, list):
        raise ValueError("'actions' must be a list")

    return {a["id"]: a for a in actions}


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def merge_params(default_params: JsonDict, params: JsonDict) -> JsonDict:
    out = dict(default_params or {})
    out.update(params or {})
    return out


def render_template(template_path: Path, params: JsonDict) -> str:
    """
    Minimal template renderer.
    Replaces {{key}} with value.
    """
    text = template_path.read_text(encoding="utf-8")
    for k, v in params.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


# ---------------------------------------------------------
# Build Overpass query
# ---------------------------------------------------------

def build_query(
    actions_path: str,
    action_id: str,
    params: JsonDict,
) -> Tuple[ActionSpec, JsonDict, str]:
    """
    Returns:
      (ActionSpec, merged_params, query_text)
    """
    actions = load_actions(actions_path)

    if action_id not in actions:
        raise KeyError(
            f"Unknown action_id={action_id}. "
            f"Available: {list(actions.keys())}"
        )

    raw = actions[action_id]

    actions_dir = Path(actions_path).parent
    template_path = actions_dir / raw["template"]

    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")

    spec = ActionSpec(
        id=raw["id"],
        template_path=template_path,
        default_params=raw.get("default_params", {}) or {},
        outputs=raw.get("outputs", []) or [],
    )

    merged_params = merge_params(spec.default_params, params or {})

    # Minimal validation (Phase 1 safe)
    if "bbox" in raw.get("params_schema", {}) and "bbox" not in merged_params:
        raise ValueError(f"Action '{action_id}' requires param: bbox")

    query_text = render_template(spec.template_path, merged_params)

    return spec, merged_params, query_text
