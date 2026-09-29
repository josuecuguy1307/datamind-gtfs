from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Literal, Sequence, Union

import streamlit as st


CardTone = Literal["normal", "warning", "critical"]
CardAlign = Literal["left", "center", "right"]


@dataclass(frozen=True)
class CardSpec:
    title: str
    value: Union[str, int, float]
    subtitle: Optional[str] = None
    delta: Optional[Union[str, int, float]] = None
    tone: CardTone = "normal"
    align: CardAlign = "left"
    tag: Optional[str] = None


def _to_str(x: Union[str, int, float]) -> str:
    if isinstance(x, float):
        if abs(x) >= 1000:
            return f"{x:,.0f}"
        return f"{x:.3f}".rstrip("0").rstrip(".")
    if isinstance(x, int):
        return f"{x:,}"
    return str(x)


def _tone_color(tone: CardTone) -> str:
    if tone == "critical":
        return "rgba(255, 80, 80, 0.18)"
    if tone == "warning":
        return "rgba(255, 200, 80, 0.18)"
    return "rgba(255, 255, 255, 0.08)"


def render_card(spec: CardSpec) -> None:
    bg = _tone_color(spec.tone)
    value_str = _to_str(spec.value)

    delta_str = None
    if spec.delta is not None:
        delta_str = _to_str(spec.delta)

    align_css = {
        "left": "left",
        "center": "center",
        "right": "right",
    }[spec.align]

    title = spec.title
    subtitle = spec.subtitle or ""
    tag = spec.tag

    html = f"""
    <div style="
        background: {bg};
        border: 1px solid rgba(255,255,255,0.10);
        border-radius: 18px;
        padding: 14px 16px;
        width: 100%;
        box-sizing: border-box;
        text-align: {align_css};
    ">
      <div style="
          display:flex;
          justify-content: space-between;
          align-items: center;
          gap: 10px;
          margin-bottom: 6px;
      ">
        <div style="
            font-size: 12px;
            opacity: 0.85;
            letter-spacing: 0.25px;
            font-weight: 600;
        ">{title}</div>
        {"<div style='font-size:11px; opacity:0.7; padding:3px 8px; border-radius:999px; border:1px solid rgba(255,255,255,0.12);'>" + str(tag) + "</div>" if tag else ""}
      </div>

      <div style="
          font-size: 26px;
          font-weight: 800;
          line-height: 1.15;
          margin-bottom: 4px;
      ">{value_str}</div>

      <div style="
          display:flex;
          justify-content: space-between;
          align-items: baseline;
          gap: 10px;
      ">
        <div style="font-size: 12px; opacity: 0.70;">{subtitle}</div>
        {"<div style='font-size:12px; opacity:0.85; font-weight:650;'>" + str(delta_str) + "</div>" if delta_str else ""}
      </div>
    </div>
    """
    st.markdown(html, unsafe_allow_html=True)


def render_card_row(cards: Sequence[CardSpec], columns: Optional[int] = None) -> None:
    if not cards:
        return

    n = columns if columns is not None else len(cards)
    n = max(1, min(n, len(cards)))
    cols = st.columns(n)

    for i, spec in enumerate(cards):
        with cols[i % n]:
            render_card(spec)


def render_kpi_strip(
    title: str,
    cards: Sequence[CardSpec],
    columns: Optional[int] = None,
) -> None:
    st.markdown(f"### {title}")
    render_card_row(cards, columns=columns)
