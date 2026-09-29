"""
STOP vs POI – LightGBM training script (Phase 1)

This file is NOT used by the pipeline.
It is executed manually when enough labeled data exists.
"""

from __future__ import annotations

import pandas as pd


def load_training_data(conn) -> pd.DataFrame:
    """
    Pull labeled STOP / POI data from DB.
    To be implemented once human labels exist.
    """
    raise NotImplementedError("Training data loader not implemented yet.")


def train_model(df: pd.DataFrame, out_path: str):
    """
    Train LightGBM binary classifier and save model artifact.
    """
    raise NotImplementedError("Training logic not implemented yet.")


if __name__ == "__main__":
    raise SystemExit(
        "This training script is a stub. "
        "Implement once labeled STOP/POI data exists."
    )
