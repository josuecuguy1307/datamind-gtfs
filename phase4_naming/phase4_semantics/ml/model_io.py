"""
Phase 4 – Model I/O (serialization boundary)

This module is responsible ONLY for saving and loading
trained ranking models.

NO training
NO inference logic
NO feature transforms
"""

from typing import Any, Dict
import joblib
import json
from pathlib import Path


# -------------------------------------------------
# Paths & conventions
# -------------------------------------------------

DEFAULT_MODEL_FILENAME = "ranker.joblib"
DEFAULT_META_FILENAME = "ranker_meta.json"


# -------------------------------------------------
# Save model
# -------------------------------------------------

def save_model(
    model: Any,
    output_dir: str,
    feature_names: list,
    model_version: str = "v1",
):
    """
    Persist a trained ranking model and its metadata.

    Args:
        model: trained model object (must be joblib-serializable)
        output_dir: directory to store model files
        feature_names: ordered list of feature names
        model_version: semantic version string
    """

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Save model (parameters only)
    joblib.dump(model, out / DEFAULT_MODEL_FILENAME)

    # Save metadata (NOT part of the model function)
    meta: Dict[str, Any] = {
        "model_version": model_version,
        "feature_names": feature_names,
    }

    with open(out / DEFAULT_META_FILENAME, "w") as f:
        json.dump(meta, f, indent=2)


# -------------------------------------------------
# Load model
# -------------------------------------------------

def load_model(
    model_dir: str,
):
    """
    Load a trained ranking model and its metadata.

    Returns:
        model: loaded model
        meta : metadata dict (feature order, version info)
    """

    base = Path(model_dir)

    model = joblib.load(base / DEFAULT_MODEL_FILENAME)

    with open(base / DEFAULT_META_FILENAME, "r") as f:
        meta = json.load(f)

    return model, meta
