"""Design tokens: config.toml ↔ theme.py sync and WCAG contrast."""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from datamind_console.ui import theme

CONFIG = tomllib.loads(Path(".streamlit/config.toml").read_text(encoding="utf-8"))


def _lum(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def blend(top: str, bottom: str, alpha: float) -> str:
    t = [int(top.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    b = [int(bottom.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(f"{round(alpha * x + (1 - alpha) * y):02X}" for x, y in zip(t, b))


@pytest.mark.parametrize("kind,palette", [("dark", theme.NIGHT), ("light", theme.PAPER)])
def test_config_toml_matches_python_tokens(kind: str, palette: theme.Palette) -> None:
    cfg = CONFIG["theme"][kind]
    assert cfg["base"] == kind
    assert cfg["primaryColor"].upper() == palette.primary.upper()
    assert cfg["backgroundColor"].upper() == palette.background.upper()
    assert cfg["secondaryBackgroundColor"].upper() == palette.surface.upper()
    assert cfg["textColor"].upper() == palette.text.upper()
    assert cfg["linkColor"].upper() == palette.link.upper()
    assert cfg["borderColor"].upper() == palette.border.upper()
    assert CONFIG["theme"][kind]["sidebar"]["backgroundColor"].upper() == palette.sidebar.upper()
    assert [c.upper() for c in cfg["chartCategoricalColors"]] == [c.upper() for c in palette.chart_series]


@pytest.mark.parametrize("palette", [theme.NIGHT, theme.PAPER], ids=["night", "paper"])
def test_text_pairs_meet_wcag_aa(palette: theme.Palette) -> None:
    pairs = {
        "text/background": (palette.text, palette.background),
        "text/surface": (palette.text, palette.surface),
        "text/sidebar": (palette.text, palette.sidebar),
        "muted/background": (palette.text_muted, palette.background),
        "muted/surface": (palette.text_muted, palette.surface),
        "on_primary/primary": (palette.on_primary, palette.primary),
        "link/background": (palette.link, palette.background),
        "accent_text/background": (palette.accent_text, palette.background),
    }
    failures = {name: round(contrast(fg, bg), 2) for name, (fg, bg) in pairs.items() if contrast(fg, bg) < 4.5}
    assert not failures, f"WCAG AA (4.5:1) failures: {failures}"


@pytest.mark.parametrize("kind,alpha", [("dark", 0.20), ("light", 0.10)])
def test_status_badge_text_is_readable(kind: str, alpha: float) -> None:
    cfg = CONFIG["theme"][kind]
    page = cfg["backgroundColor"]
    failures = {}
    for color in ("green", "red", "orange", "yellow", "blue", "violet", "gray"):
        badge_bg = blend(cfg[f"{color}Color"], page, alpha)
        ratio = contrast(cfg[f"{color}TextColor"], badge_bg)
        if ratio < 4.5:
            failures[color] = round(ratio, 2)
    assert not failures, f"badge text contrast < 4.5:1 in {kind}: {failures}"


def test_status_tones_map_to_streamlit_badge_colours() -> None:
    allowed = {"red", "orange", "yellow", "blue", "green", "violet", "gray", "primary"}
    assert set(theme.TONE_TO_BADGE.values()) <= allowed
