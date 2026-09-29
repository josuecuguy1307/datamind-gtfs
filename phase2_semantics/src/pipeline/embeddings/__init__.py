"""
Approval package public API.

This package exposes:
- Pure decision logic (no DB, no IO)
- Optional selection logging helpers
"""

from .embedder import embed , embed_many

__all__ = [
    "embed",
    "embed_many"
]
