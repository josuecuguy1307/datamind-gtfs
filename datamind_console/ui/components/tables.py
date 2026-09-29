from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, List, Literal, Union

import streamlit as st
import pandas as pd


Align = Literal["left", "center", "right"]


@dataclass(frozen=True)
class ColumnSpec:
    key: str
    title: str
    help: Optional[str] = None
    width: Optional[str] = None
    align: Align = "left"
    fmt: Optional[str] = None
    hide: bool = False


def _format_value(v: Any, fmt: Optional[str]) -> Any:
    if fmt is None:
        return v
    try:
        if v is None:
            return ""
        if isinstance(v, (int, float)):
            return format(v, fmt)
        return fmt.format(v)
    except Exception:
        return v


def make_dataframe(
    rows: Sequence[Dict[str, Any]],
    columns: Sequence[ColumnSpec],
) -> pd.DataFrame:
    df = pd.DataFrame(list(rows)) if rows else pd.DataFrame()

    if df.empty:
        for c in columns:
            if c.key not in df.columns:
                df[c.key] = []
        return df

    for c in columns:
        if c.key not in df.columns:
            df[c.key] = None

    ordered_keys = [c.key for c in columns]
    df = df[ordered_keys]

    for c in columns:
        if c.fmt is not None and c.key in df.columns:
            df[c.key] = df[c.key].apply(lambda x: _format_value(x, c.fmt))

    return df


def render_table(
    title: str,
    df: pd.DataFrame,
    columns: Optional[Sequence[ColumnSpec]] = None,
    height: int = 420,
    use_container_width: bool = True,
) -> None:
    st.markdown(f"#### {title}")

    if df is None or df.empty:
        st.info("No rows to display.")
        return

    if columns:
        show_keys = [c.key for c in columns if not c.hide]
        df_show = df[show_keys].copy()
        st.dataframe(df_show, use_container_width=use_container_width, height=height)
        return

    st.dataframe(df, use_container_width=use_container_width, height=height)


def render_ranked_table(
    title: str,
    df: pd.DataFrame,
    score_col: str = "score",
    top_n: int = 25,
    height: int = 420,
) -> None:
    st.markdown(f"#### {title}")

    if df is None or df.empty:
        st.info("No rows to display.")
        return

    if score_col in df.columns:
        df = df.sort_values(score_col, ascending=False)

    df_show = df.head(top_n)
    st.dataframe(df_show, use_container_width=True, height=height)


def select_row(
    title: str,
    df: pd.DataFrame,
    id_col: str,
    label_cols: Optional[Sequence[str]] = None,
    default_index: int = 0,
) -> Optional[Any]:
    st.markdown(f"#### {title}")

    if df is None or df.empty:
        st.info("No items to select.")
        return None

    if id_col not in df.columns:
        st.error(f"Missing id column: {id_col}")
        return None

    if label_cols is None:
        label_cols = [id_col]

    options: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        label_parts = []
        for c in label_cols:
            if c in df.columns:
                label_parts.append(str(row[c]))
        label = " | ".join(label_parts)
        options.append({"id": row[id_col], "label": label})

    labels = [o["label"] for o in options]
    idx = max(0, min(default_index, len(labels) - 1))

    chosen_label = st.selectbox("Select", labels, index=idx)
    chosen = next((o for o in options if o["label"] == chosen_label), None)
    return chosen["id"] if chosen else None


def render_key_value_table(
    title: str,
    data: Dict[str, Any],
) -> None:
    st.markdown(f"#### {title}")

    if not data:
        st.info("No data.")
        return

    rows = [{"key": k, "value": v} for k, v in data.items()]
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, height=min(420, 60 + 35 * len(rows)))


def render_hotspot_table(
    title: str,
    rows: Sequence[Dict[str, Any]],
    key_cols: Sequence[str],
    score_col: str,
    top_n: int = 20,
) -> None:
    st.markdown(f"#### {title}")

    if not rows:
        st.info("No hotspots.")
        return

    df = pd.DataFrame(list(rows))

    if score_col in df.columns:
        df = df.sort_values(score_col, ascending=False)

    cols = [c for c in key_cols if c in df.columns]
    if score_col in df.columns:
        cols.append(score_col)

    df_show = df[cols].head(top_n)
    st.dataframe(df_show, use_container_width=True, height=460)
