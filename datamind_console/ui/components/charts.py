from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Union, Literal, List, Dict, Any

import streamlit as st
import pandas as pd
import matplotlib.pyplot as plt


ChartKind = Literal["line", "bar", "hist", "scatter"]
Number = Union[int, float]


@dataclass(frozen=True)
class SeriesSpec:
    name: str
    x: Sequence[Any]
    y: Sequence[Number]


def _figure(figsize: tuple[float, float] = (6.8, 3.6)) -> plt.Figure:
    fig = plt.figure(figsize=figsize)
    return fig


def _safe_df(series: Sequence[SeriesSpec]) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for s in series:
        for xi, yi in zip(s.x, s.y):
            rows.append({"series": s.name, "x": xi, "y": yi})
    return pd.DataFrame(rows)


def line_chart(
    title: str,
    series: Sequence[SeriesSpec],
    x_label: str = "",
    y_label: str = "",
    figsize: tuple[float, float] = (7.2, 3.6),
) -> None:
    if not series:
        st.info("No data.")
        return

    df = _safe_df(series)

    fig = _figure(figsize=figsize)
    ax = plt.gca()

    for name in df["series"].unique():
        sub = df[df["series"] == name]
        ax.plot(sub["x"], sub["y"], marker="o", linewidth=1.6, label=name)

    ax.set_title(title)
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)

    if len(df["series"].unique()) > 1:
        ax.legend(frameon=False)

    ax.grid(True, alpha=0.25)
    st.pyplot(fig)


def bar_chart(
    title: str,
    categories: Sequence[Any],
    values: Sequence[Number],
    x_label: str = "",
    y_label: str = "",
    figsize: tuple[float, float] = (7.2, 3.6),
    rotate_xticks: bool = True,
) -> None:
    if not categories or not values:
        st.info("No data.")
        return

    fig = _figure(figsize=figsize)
    ax = plt.gca()
    ax.bar(list(categories), list(values))

    ax.set_title(title)
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)

    if rotate_xticks:
        plt.xticks(rotation=25, ha="right")

    ax.grid(True, axis="y", alpha=0.25)
    st.pyplot(fig)


def histogram(
    title: str,
    values: Sequence[Number],
    bins: int = 30,
    x_label: str = "",
    y_label: str = "Count",
    figsize: tuple[float, float] = (7.2, 3.6),
) -> None:
    if not values:
        st.info("No data.")
        return

    fig = _figure(figsize=figsize)
    ax = plt.gca()
    ax.hist(list(values), bins=bins)

    ax.set_title(title)
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)

    ax.grid(True, axis="y", alpha=0.25)
    st.pyplot(fig)


def scatter_plot(
    title: str,
    x: Sequence[Number],
    y: Sequence[Number],
    x_label: str = "",
    y_label: str = "",
    figsize: tuple[float, float] = (7.2, 3.6),
) -> None:
    if not x or not y:
        st.info("No data.")
        return

    fig = _figure(figsize=figsize)
    ax = plt.gca()
    ax.scatter(list(x), list(y), s=30)

    ax.set_title(title)
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)

    ax.grid(True, alpha=0.25)
    st.pyplot(fig)


def dataframe_preview(
    title: str,
    df: pd.DataFrame,
    height: int = 320,
) -> None:
    st.markdown(f"#### {title}")
    st.dataframe(df, use_container_width=True, height=height)
