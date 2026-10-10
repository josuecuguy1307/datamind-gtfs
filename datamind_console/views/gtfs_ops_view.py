from __future__ import annotations

from typing import Any

import streamlit as st

from datamind_console.i18n import t
from datamind_console.ui import navigation
from datamind_console.ui.components.page import page_header
from datamind_console.views.gtfs_exports_view import render_gtfs_exports_section
from datamind_console.widgets.gtfs_ops_widget import render_gtfs_ops_widget


def render_gtfs_ops_view(*, analytics: Any = None, audit: Any = None, **_) -> None:
    page_header(t("page.gtfs.title"), t("page.gtfs.subtitle"), eyebrow=t("page.gtfs.eyebrow"))
    with st.container(horizontal=True, gap="medium"):
        navigation.link("phases", t("gtfs.link.build_phase5"), icon=":material/build:", phase=5)
        navigation.link("revise_gtfs", t("gtfs.link.revise"), icon=":material/edit_road:")

    tab_ops, tab_exports = st.tabs([t("gtfs.tab.validate_publish"), t("gtfs.tab.exports")])
    with tab_ops:
        render_gtfs_ops_widget()
    with tab_exports:
        render_gtfs_exports_section()
