from __future__ import annotations

from typing import List, Sequence
import threading

from sentence_transformers import SentenceTransformer

from src.settings import (
    EMBED_MODEL_NAME,
    EMBED_DIM,
    EMBED_BATCH_SIZE,
)


# Singleton model (thread-safe lazy init)
_model: SentenceTransformer | None = None
_model_lock = threading.Lock()


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                _model = SentenceTransformer(EMBED_MODEL_NAME)
                _model.eval()
    return _model


def _to_float_list(vec) -> List[float]:
    # vec is np.ndarray normally
    out = [float(x) for x in vec.tolist()]
    if EMBED_DIM and len(out) != EMBED_DIM:
        raise ValueError(
            f"Embedding dim mismatch: got {len(out)} but settings.EMBED_DIM={EMBED_DIM}. "
            f"Model: {EMBED_MODEL_NAME}"
        )
    return out


def embed(text: str) -> List[float]:
    """
    Embed a single text into a unit-length vector.

    Guarantees:
    - deterministic (CPU)
    - normalized embeddings (cosine-ready)
    """
    if not text or not text.strip():
        raise ValueError("Cannot embed empty text")

    model = _get_model()

    vec = model.encode(
        text,
        normalize_embeddings=True,
        show_progress_bar=False,
    )

    return _to_float_list(vec)


def embed_many(texts: Sequence[str]) -> List[List[float]]:
    """
    Batch embed (FAST).
    - Keeps same guarantees.
    - Uses settings.EMBED_BATCH_SIZE
    """
    if not texts:
        return []

    cleaned = []
    for t in texts:
        if not t or not str(t).strip():
            cleaned.append(" ")  # keep shape stable
        else:
            cleaned.append(str(t))

    model = _get_model()

    vecs = model.encode(
        cleaned,
        batch_size=EMBED_BATCH_SIZE,
        normalize_embeddings=True,
        show_progress_bar=False,
    )

    return [_to_float_list(v) for v in vecs]
