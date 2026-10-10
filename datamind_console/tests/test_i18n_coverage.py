"""Every visible string in the console goes through the i18n catalog.

Three guards:
* no function rebinds ``t`` locally, which would shadow the translator and
  raise ``UnboundLocalError`` at the first ``t(...)`` call;
* no Streamlit call (title, caption, button, metric, column labels, ...) gets
  a natural-language literal directly;
* no option widget shows literal options (words, status codes or sentinels
  such as ``"(all)"``) without a ``format_func``.

Allowed as-is: technical identifiers (snake_case, dotted names such as
``trip_id`` or ``catalog.route_semantics``), which read the same in every
language. Operational log streams, audit notes written to the database, API
examples and shell snippets are not Streamlit labels and are out of scope.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOTS = [Path("datamind_console/views"), Path("datamind_console/widgets"), Path("datamind_console/ui"), Path("datamind_console/app.py")]
UI_CALLS = {
    "markdown", "caption", "write", "button", "subheader", "header", "title", "info", "warning", "error", "success",
    "checkbox", "selectbox", "text_input", "expander", "tabs", "radio", "metric", "number_input", "text_area", "toggle",
    "multiselect", "download_button", "form_submit_button", "slider", "select_slider", "file_uploader", "date_input",
    "time_input", "popover", "status", "spinner", "toast", "link_button", "page_link", "segmented_control", "pills",
    "TextColumn", "NumberColumn", "CheckboxColumn", "LinkColumn", "ProgressColumn", "DatetimeColumn",
    "SelectboxColumn", "ListColumn", "JsonColumn", "Column", "DateColumn",
}
LABEL_KWARGS = {"label", "help", "placeholder", "caption", "title", "body", "text"}
OPTION_WIDGETS = {"radio", "selectbox", "segmented_control", "multiselect", "pills", "select_slider"}
IDENTIFIER = re.compile(r"[a-z0-9]+(?:[_.:/-][a-z0-9]+)*")


def _files():
    for root in ROOTS:
        yield from (root.rglob("*.py") if root.is_dir() else [root])


def _literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return None


def _is_prose(text: str) -> bool:
    return bool(re.search(r"[A-Za-zÁÉÍÓÚáéíóúñ]{3,}", text)) and not IDENTIFIER.fullmatch(text.strip())


def _call_name(node: ast.Call) -> tuple[str | None, ast.AST | None]:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr, f.value
    return (f.id if isinstance(f, ast.Name) else None), None


def test_no_literal_text_in_streamlit_calls() -> None:
    offenders = []
    for path in _files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name, receiver = _call_name(node)
            if name not in UI_CALLS:
                continue
            if isinstance(receiver, ast.Name) and re.search("log", receiver.id, re.I):
                continue
            candidates = []
            if node.args:
                first = node.args[0]
                candidates += list(first.elts) if name == "tabs" and isinstance(first, (ast.List, ast.Tuple)) else [first]
            candidates += [k.value for k in node.keywords if k.arg in LABEL_KWARGS]
            for c in candidates:
                text = _literal(c)
                if text and _is_prose(text):
                    offenders.append(f"{path}:{node.lineno} {text[:50]!r}")
    assert not offenders, "\n".join(offenders)


def test_option_widgets_translate_literal_options() -> None:
    offenders = []
    for path in _files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name, _ = _call_name(node)
            if name not in OPTION_WIDGETS:
                continue
            options = next((k.value for k in node.keywords if k.arg == "options"), node.args[1] if len(node.args) > 1 else None)
            if options is None:
                continue
            # Any word literal in the options expression is shown to the user:
            # prose, status codes ("pending") and sentinels ("(all)") alike.
            words = [
                c.value for c in ast.walk(options)
                if isinstance(c, ast.Constant) and isinstance(c.value, str) and re.search(r"[A-Za-z]{2,}", c.value)
            ]
            if words and not any(k.arg == "format_func" for k in node.keywords):
                offenders.append(f"{path}:{node.lineno} {words[:2]}")
    assert not offenders, "\n".join(offenders)


def _local_bindings_and_calls(fn: ast.AST) -> tuple[list[int], bool]:
    """Lines that bind ``t`` inside ``fn`` (not nested scopes) and whether ``fn`` calls ``t(...)``."""
    binds, calls = [], False
    params = fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs
    if any(a.arg == "t" for a in params):
        binds.append(fn.lineno)
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.comprehension)):
            continue
        if isinstance(node, ast.Name) and node.id == "t" and isinstance(node.ctx, ast.Store):
            binds.append(node.lineno)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "t":
            calls = True
        stack.extend(ast.iter_child_nodes(node))
    return binds, calls


def test_no_function_shadows_the_translator() -> None:
    offenders = []
    for path in Path("datamind_console").rglob("*.py"):
        for fn in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                binds, calls = _local_bindings_and_calls(fn)
                if binds and calls:
                    offenders.append(f"{path}:{binds[0]} {fn.name}")
    assert not offenders, "\n".join(offenders)


def test_no_raw_tracebacks_in_the_ui() -> None:
    """Errors go through ``error_state``: a translated message, details folded away."""
    offenders = []
    for path in _files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and _call_name(node)[0] == "exception":
                receiver = _call_name(node)[1]
                if isinstance(receiver, ast.Name) and receiver.id == "st":
                    offenders.append(f"{path}:{node.lineno}")
    assert not offenders, "\n".join(offenders)
