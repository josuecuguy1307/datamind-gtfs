"""Load and render phase panels (widgets and tabs) without hiding failures.

Each phase view used to define its own ``_load`` (byte-identical ×5), which
swallowed import errors and returned ``None``, so a broken panel silently
vanished from the page. ``_call`` printed raw tracebacks with
``st.exception``. Both now go through the shared error state: the operator
sees what is unavailable, and the details stay in an expander and the log.
"""
from __future__ import annotations

import importlib
import inspect
import logging
from typing import Any, Callable, Optional

LOGGER = logging.getLogger("datamind_console.views.phases")


def _failed_panel(module_path: str, fn_name: str, exc: BaseException) -> Callable[..., None]:
    def _render(**_: Any) -> None:
        from datamind_console.ui.components.states import error_state

        error_state(exc, context_key="error.panel_unavailable", panel=f"{module_path}.{fn_name}", retry=False)

    _render.__name__ = f"unavailable_{fn_name}"
    return _render


def load_panel(module_path: str, fn_name: str) -> Optional[Callable[..., Any]]:
    """Import ``module_path.fn_name``; on failure return a panel that explains it."""
    try:
        mod = importlib.import_module(module_path)
    except Exception as exc:  # noqa: BLE001 - surfaced in the UI and the log
        LOGGER.exception("panel.import_failed module=%s fn=%s", module_path, fn_name)
        return _failed_panel(module_path, fn_name, exc)
    fn = getattr(mod, fn_name, None)
    if not callable(fn):
        exc = AttributeError(f"{module_path} has no callable {fn_name}")
        LOGGER.error("panel.missing module=%s fn=%s", module_path, fn_name)
        return _failed_panel(module_path, fn_name, exc)
    return fn


def call_panel(render_fn: Optional[Callable[..., Any]], *, ctx: Any = None, **deps: Any) -> Any:
    """Call a panel with only the keyword arguments it accepts."""
    if render_fn is None:
        return None
    try:
        params = inspect.signature(render_fn).parameters
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return render_fn(ctx=ctx, **deps)
        safe = {k: v for k, v in deps.items() if k in params}
        if "ctx" in params:
            return render_fn(ctx=ctx, **safe)
        return render_fn(**safe)
    except Exception as exc:  # noqa: BLE001 - surfaced in the UI and the log
        from datamind_console.ui.components.states import error_state

        error_state(exc, context_key="error.panel_failed", panel=getattr(render_fn, "__name__", "panel"), retry=False)
        return None
