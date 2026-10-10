"""Loading, empty and error states with product language.

Operators see a short explanation and what to do next. Developers get the
technical details in an expander. Full tracebacks always go to the log.
Errors are never hidden: every caught exception is rendered and logged.
"""
from __future__ import annotations

import logging
import traceback
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional, TypeVar

import streamlit as st

from datamind_console.i18n import t

LOGGER = logging.getLogger("datamind_console.ui")
T = TypeVar("T")


@contextmanager
def loading(message_key: str = "state.loading.generic", **params: Any) -> Iterator[None]:
    with st.spinner(t(message_key, **params), show_time=True):
        yield


def empty_state(message_key: str, *, hint_key: Optional[str] = None, icon: str = ":material/inbox:", **params: Any) -> None:
    st.info(t(message_key, **params), icon=icon)
    if hint_key:
        st.caption(t(hint_key, **params))


def _classify(exc: BaseException) -> str:
    from datamind_console.db.schema_guard import SchemaNotReadyError

    if isinstance(exc, SchemaNotReadyError):
        return "schema_not_ready"
    try:
        import psycopg2
        import psycopg2.errors

        if isinstance(exc, psycopg2.errors.ReadOnlySqlTransaction):
            return "read_only"
        if isinstance(exc, psycopg2.OperationalError):
            return "db_unavailable"
        if isinstance(exc, psycopg2.errors.QueryCanceled):
            return "timeout"
    except Exception:  # pragma: no cover - psycopg2 always installed
        pass
    message = str(exc)
    if isinstance(exc, RuntimeError) and ("DSN" in message or "database" in message.lower()):
        return "db_unavailable"
    return "generic"


def error_state(
    exc: BaseException,
    *,
    context_key: str = "error.generic.title",
    retry: bool = True,
    key: Optional[str] = None,
    **params: Any,
) -> None:
    """Render a recoverable error: what happened, what to do, technical details."""
    kind = _classify(exc)
    LOGGER.error("ui.error kind=%s context=%s: %s", kind, context_key, exc, exc_info=exc)

    if kind == "schema_not_ready":
        st.warning(t("error.schema_not_ready.title", unit=getattr(exc, "unit_title", "")), icon=":material/database:")
        st.caption(t("error.schema_not_ready.body"))
        st.code(f"{getattr(exc, 'plan_command', '')}\n{getattr(exc, 'apply_command', '')}", language="bash")
    elif kind == "db_unavailable":
        st.error(t("error.db_unavailable.title"), icon=":material/cloud_off:")
        st.caption(t("error.db_unavailable.body"))
    elif kind == "read_only":
        st.error(t("error.read_only.title"), icon=":material/lock:")
        st.caption(t("error.read_only.body"))
    elif kind == "timeout":
        st.warning(t("error.timeout.title"), icon=":material/timer_off:")
        st.caption(t("error.timeout.body"))
    else:
        st.error(t(context_key, **params), icon=":material/error:")
        st.caption(t("error.generic.body"))

    with st.expander(t("error.technical_details"), expanded=False):
        st.code(
            f"{type(exc).__name__}: {exc}\n\n" + "".join(traceback.format_exception(exc)[-12:]),
            language="text",
        )
    if retry and st.button(t("action.retry"), key=key or f"retry.{context_key}.{id(exc)}", icon=":material/refresh:"):
        st.rerun()


def guarded(fn: Callable[..., T], *args: Any, context_key: str = "error.generic.title", **kwargs: Any) -> Optional[T]:
    """Run a rendering function; on error show :func:`error_state` instead of a traceback."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - rendered and logged
        error_state(exc, context_key=context_key)
        return None
