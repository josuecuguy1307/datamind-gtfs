"""Shared pieces of the human review screens: who is deciding, and the log."""
from __future__ import annotations

from typing import Any, Optional

import pandas as pd
import streamlit as st

from datamind_console.i18n import t
from datamind_console.review.human_review import Reviewer
from datamind_console.ui.formatting import format_date

_REVIEWER_KEY = "review.reviewer_name"


def reviewer_identity() -> Reviewer:
    """The signed-in user, or (local mode, no accounts) a name the reviewer types.

    The name is kept for the session and written into every audit row.
    """
    user = st.session_state.get("auth.user") or {}
    if user.get("user_id"):
        name = user.get("display_name") or user.get("email") or ""
        st.caption(t("hreview.reviewer.signed_in", name=name))
        return Reviewer(name=str(name), user_id=str(user["user_id"]))
    name = st.text_input(
        t("hreview.reviewer.label"),
        key=_REVIEWER_KEY,
        placeholder=t("hreview.reviewer.placeholder"),
        help=t("hreview.reviewer.help"),
        max_chars=60,
    )
    return Reviewer(name=name or "")


def current_reviewer() -> Reviewer:
    """The reviewer already entered on this page, without drawing the field again.

    For a second decision area on the same page: ``reviewer_identity`` can be
    rendered only once per run (its widget key is shared).
    """
    user = st.session_state.get("auth.user") or {}
    if user.get("user_id"):
        name = user.get("display_name") or user.get("email") or ""
        return Reviewer(name=str(name), user_id=str(user["user_id"]))
    return Reviewer(name=str(st.session_state.get(_REVIEWER_KEY) or ""))


def decision_log(rows: list[dict[str, Any]], *, empty_key: str) -> None:
    if not rows:
        st.caption(t(empty_key))
        return
    table = pd.DataFrame(
        {
            t("col.when"): [format_date(r["created_at"], with_time=True) for r in rows],
            t("col.item"): [str(r["item_id"]).split(":", 1)[-1][:8] for r in rows],
            t("col.decision"): [t(f"hreview.decision.{str(r['decision']).lower()}") for r in rows],
            t("col.reason_code"): [t(f"hreview.reason_code.{r['reason_code']}") if r.get("reason_code") else "—" for r in rows],
            t("col.notes"): [r.get("notes") or "" for r in rows],
        }
    )
    st.dataframe(table, hide_index=True, width="stretch")


def flash(key: str, message: Optional[str] = None) -> None:
    """Set (message given) or show-and-clear (no message) a one-shot notice."""
    if message is not None:
        st.session_state[key] = message
        return
    pending = st.session_state.pop(key, None)
    if pending:
        st.success(pending, icon=":material/task_alt:")
