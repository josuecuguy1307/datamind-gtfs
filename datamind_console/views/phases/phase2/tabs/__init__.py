# views/phases/phase2/tabs/__init__.py

from __future__ import annotations

from .extract_tab import render_extract_tab
from .candidates_tab import render_candidates_tab
from .approve_tab import render_approve_tab

__all__ = [
    "render_extract_tab",
    "render_candidates_tab",
    "render_approve_tab",
]
