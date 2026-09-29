from __future__ import annotations

import re
from typing import Any


_UI_ES = {
    "Navigation": "Navegacion",
    "Dashboard": "Panel",
    "Phases": "Fases",
    "Geo API": "Geo API",
    "Revise Current GTFS": "Revisar GTFS Actual",
    "Training": "Entrenamiento",
    "Insights": "Insights",
    "Settings": "Configuracion",
    "Login required": "Inicio de sesion requerido",
    "Email": "Correo",
    "Password": "Contrasena",
    "Sign in": "Iniciar sesion",
    "Enter email and password.": "Ingresa correo y contrasena.",
    "Invalid credentials.": "Credenciales invalidas.",
    "Logout": "Cerrar sesion",
    "Refresh": "Actualizar",
    "Run Valhalla": "Ejecutar Valhalla",
    "Confirm": "Confirmar",
    "Confirm approval": "Confirmar aprobacion",
    "Confirm change": "Confirmar cambio",
    "Confirm new position": "Confirmar nueva posicion",
    "Approve node": "Aprobar nodo",
    "Reject node": "Rechazar nodo",
    "Delete node": "Eliminar nodo",
    "Move": "Mover",
    "Cancel": "Cancelar",
    "Workspace": "Espacio de trabajo",
    "Workspace Nodes": "Nodos Workspace",
    "Workspace fullscreen mode": "Modo pantalla completa del workspace",
    "Exit workspace fullscreen": "Salir de pantalla completa",
    "Go to Dashboard": "Ir al panel",
    "New Nodes": "Nuevos Nodos",
    "Steps": "Pasos",
    "Map Preview": "Vista de mapa",
    "Edit StopTimes": "Editar StopTimes",
    "Edit Shape Points": "Editar puntos de shape",
    "Edit Stops (name + lat/lon)": "Editar paradas (nombre + lat/lon)",
    "Route Geometry (Valhalla from Phase 3)": "Geometria de ruta (Valhalla desde Fase 3)",
    "Language": "Idioma",
    "English": "Ingles",
    "Spanish": "Espanol",
}


def _translate_dynamic(text: str) -> str:
    s = text

    s = re.sub(r"^Run Step\s+(\d+)\b", r"Ejecutar Paso \1", s)
    s = re.sub(r"^Step\s+(\d+)\s+completed\.?$", r"Paso \1 completado.", s)

    s = re.sub(r"^Select\s+", "Seleccionar ", s)
    s = re.sub(r"^Load\s+", "Cargar ", s)
    s = re.sub(r"^Update\s+", "Actualizar ", s)

    s = s.replace("Confirm and update", "Confirmar y actualizar")
    s = s.replace("Confirm new", "Confirmar nueva")
    s = s.replace("No rows available.", "No hay filas disponibles.")
    s = s.replace("No results.", "Sin resultados.")

    return s


def t(text: Any, lang: str = "en") -> Any:
    if lang != "es":
        return text
    if not isinstance(text, str):
        return text
    if text in _UI_ES:
        return _UI_ES[text]
    return _translate_dynamic(text)


def get_lang(ss: Any) -> str:
    return str(ss.get("ui.lang") or "en")


def set_lang(ss: Any, lang: str) -> None:
    ss["ui.lang"] = "es" if str(lang).lower().startswith("es") else "en"


def apply_streamlit_i18n(st_module: Any, ss: Any) -> None:
    if getattr(st_module, "_datamind_i18n_patched", False):
        return

    orig = {}

    def _label_first(fn_name: str):
        fn = getattr(st_module, fn_name)
        orig[fn_name] = fn

        def wrapped(*args, **kwargs):
            lang = get_lang(ss)
            if args:
                a0 = t(args[0], lang)
                args = (a0, *args[1:])
            elif "label" in kwargs:
                kwargs["label"] = t(kwargs["label"], lang)
            if "help" in kwargs and isinstance(kwargs["help"], str):
                kwargs["help"] = t(kwargs["help"], lang)
            return fn(*args, **kwargs)

        return wrapped

    def _text_only(fn_name: str):
        fn = getattr(st_module, fn_name)
        orig[fn_name] = fn

        def wrapped(body, *args, **kwargs):
            return fn(t(body, get_lang(ss)), *args, **kwargs)

        return wrapped

    for name in [
        "button",
        "form_submit_button",
        "text_input",
        "text_area",
        "number_input",
        "checkbox",
        "selectbox",
        "multiselect",
        "radio",
        "file_uploader",
        "metric",
    ]:
        if hasattr(st_module, name):
            setattr(st_module, name, _label_first(name))

    for name in ["title", "header", "subheader", "caption", "info", "warning", "success", "error", "markdown"]:
        if hasattr(st_module, name):
            setattr(st_module, name, _text_only(name))

    if hasattr(st_module, "tabs"):
        fn_tabs = st_module.tabs
        orig["tabs"] = fn_tabs

        def wrapped_tabs(labels, *args, **kwargs):
            lang = get_lang(ss)
            try:
                labels = [t(x, lang) for x in labels]
            except Exception:
                pass
            return fn_tabs(labels, *args, **kwargs)

        setattr(st_module, "tabs", wrapped_tabs)

    st_module._datamind_i18n_orig = orig
    st_module._datamind_i18n_patched = True
