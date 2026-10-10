"""Status chips: one semantic tone per status value, rendered with ``st.badge``.

Colours come from the themed Streamlit badge palette (``.streamlit/config.toml``),
so chips read correctly in both "Night" and "Paper".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

import streamlit as st

from datamind_console.i18n import status_label
from datamind_console.ui.theme import TONE_TO_BADGE

# domain -> value -> tone (success | warning | danger | info | accent | primary | neutral)
STATUS_TONES: dict[str, dict[str, str]] = {
    "cleanliness": {"clean": "success", "dirty": "warning", "unprocessed": "neutral", "__none__": "neutral"},
    "dirty_reason": {"__default__": "warning", "phase4_catalog_synthesized": "info"},
    "deploy": {
        "active": "success",
        "excluded_quality": "danger",
        "quarantined_no_stops": "danger",
        "non_sample_region": "neutral",
        "__none__": "neutral",
    },
    "queue": {"pending": "warning", "approved": "success", "rejected": "danger", "__none__": "neutral"},
    "quality": {
        "good": "success",
        "acceptable": "info",
        "ship_pending_dr": "accent",
        "degraded_minor": "warning",
        "degraded": "warning",
        "unroutable": "danger",
        "__none__": "neutral",
    },
    "reentry": {"v2_ready": "info", "swapped": "success", "failed": "danger", "pending": "neutral", "__none__": "neutral"},
    "review_state": {"pending": "warning", "approved": "success", "rejected": "danger", "__none__": "neutral"},
    "approval": {
        "pending": "neutral",
        "in_progress": "info",
        "ready": "accent",
        "approved": "success",
        "rejected": "danger",
        "__none__": "neutral",
    },
    "check": {"pass": "success", "fail": "danger", "warn": "warning", "not_run": "neutral", "unavailable": "neutral"},
    "data_mode": {"local": "info", "server": "primary"},
    "freshness": {"fresh": "success", "stale": "warning", "dormant": "neutral"},
}


@dataclass(frozen=True)
class Chip:
    label: str
    tone: str = "neutral"
    icon: Optional[str] = None
    help: Optional[str] = None


def tone_for(domain: str, value: Any) -> str:
    table = STATUS_TONES.get(domain, {})
    key = "__none__" if value in (None, "") else str(value)
    return table.get(key) or table.get("__default__") or "neutral"


def badge_color(tone: str) -> str:
    return TONE_TO_BADGE.get(tone, "gray")


def status_chip(domain: str, value: Any, *, icon: Optional[str] = None, help: Optional[str] = None) -> Chip:
    return Chip(label=status_label(domain, value), tone=tone_for(domain, value), icon=icon, help=help)


def status_badge(domain: str, value: Any, *, icon: Optional[str] = None, help: Optional[str] = None) -> None:
    chip = status_chip(domain, value, icon=icon, help=help)
    st.badge(chip.label, color=badge_color(chip.tone), icon=chip.icon, help=chip.help)


def chips_row(chips: Sequence[Chip]) -> None:
    with st.container(horizontal=True, gap="small"):
        for chip in chips:
            st.badge(chip.label, color=badge_color(chip.tone), icon=chip.icon, help=chip.help)
