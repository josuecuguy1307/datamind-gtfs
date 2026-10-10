"""DataMind design tokens.

Single source of truth for colour, spacing, radius and typography. Streamlit
widgets are themed natively from ``.streamlit/config.toml``
(``[theme.dark]`` "Night", ``[theme.light]`` "Paper"). This module mirrors
those values for the things Streamlit does not theme: pydeck maps, chart
series and the few custom HTML elements.
``datamind_console/tests/test_theme_tokens.py`` keeps both in sync and checks
WCAG contrast for every text/background pair used here.

Visual direction: deep night navy and indigo (from the DataMind mark
``#17145e``), cream/paper text and surfaces, amber for emphasis, cyan for
information and links, mint/red only for status.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ThemeType = Literal["dark", "light"]

BRAND_INDIGO = "#17145E"  # DataMind isotype background


@dataclass(frozen=True)
class Palette:
    name: str
    background: str
    sidebar: str
    surface: str  # Streamlit secondaryBackgroundColor (inputs, cards)
    surface_raised: str
    border: str
    text: str
    text_muted: str
    primary: str  # actions (primary buttons, focus)
    on_primary: str
    accent: str  # amber emphasis (active phase, brand line)
    accent_text: str  # amber usable as text on background
    link: str
    info: str
    success: str
    warning: str
    danger: str
    neutral: str
    map_style: str
    chart_series: tuple[str, ...] = field(default_factory=tuple)

    def rgba(self, hex_color: str, alpha: int = 255) -> list[int]:
        """pydeck colour ``[r, g, b, a]`` from ``#rrggbb``."""
        h = hex_color.lstrip("#")
        return [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(alpha)]


NIGHT = Palette(
    name="night",
    background="#0B1120",
    sidebar="#0E1528",
    surface="#151E33",
    surface_raised="#1B2640",
    border="#2A3654",
    text="#EEE7D5",
    text_muted="#A9B1C6",
    primary="#5B67F0",
    on_primary="#FFFFFF",
    accent="#F2B441",
    accent_text="#F2B441",
    link="#5CCBEE",
    info="#5CCBEE",
    success="#3DD68C",
    warning="#F59E3B",
    danger="#F2615C",
    neutral="#A9B1C6",
    map_style="https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
    chart_series=(
        "#F2B441", "#5CCBEE", "#3DD68C", "#8B95FF", "#F2615C",
        "#C792EA", "#E8A87C", "#7FD1B9", "#A9B1C6", "#FFD580",
    ),
)

PAPER = Palette(
    name="paper",
    background="#F7F2E6",
    sidebar="#EFE8D7",
    surface="#ECE5D4",
    surface_raised="#FFFDF6",
    border="#D3C9B2",
    text="#131B33",
    text_muted="#4C556E",
    primary="#3F4BC4",
    on_primary="#FFFFFF",
    accent="#F2B441",
    accent_text="#8A5A0F",
    link="#0C6584",
    info="#0C6584",
    success="#1C7A4B",
    warning="#9A5A06",
    danger="#B0302D",
    neutral="#4C556E",
    map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
    chart_series=(
        "#C2850F", "#0C6584", "#1C7A4B", "#3F4BC4", "#B0302D",
        "#7A4FA3", "#A8603A", "#2E8A72", "#4C556E", "#8C6D1F",
    ),
)

PALETTES: dict[str, Palette] = {"dark": NIGHT, "light": PAPER}

SPACING = {"xs": "0.25rem", "sm": "0.5rem", "md": "0.75rem", "lg": "1rem", "xl": "1.5rem", "2xl": "2rem"}
RADIUS = {"sm": "0.375rem", "md": "0.5rem", "lg": "0.75rem", "pill": "999px"}
TYPE_SCALE = {"eyebrow": "0.72rem", "caption": "0.82rem", "body": "1rem", "title": "1.9rem"}

# Semantic status tones → Streamlit badge colours (themed in config.toml).
TONE_TO_BADGE = {
    "success": "green",
    "warning": "orange",
    "danger": "red",
    "info": "blue",
    "accent": "yellow",
    "primary": "violet",
    "neutral": "gray",
}


def theme_type() -> ThemeType:
    """Active Streamlit theme type ('dark' or 'light'); dark when unknown."""
    try:
        import streamlit as st

        theme = getattr(st.context, "theme", None)
        kind = getattr(theme, "type", None) if theme is not None else None
        if kind is None and isinstance(theme, dict):
            kind = theme.get("type")
        return "light" if kind == "light" else "dark"
    except Exception:
        return "dark"


def palette() -> Palette:
    return PALETTES[theme_type()]


def is_dark() -> bool:
    return theme_type() == "dark"
