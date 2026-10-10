"""i18n catalog integrity: parity, coverage of keys used in code, placeholders.

Acts as the missing-key detector: adding ``t("new.key")`` without catalog
entries fails here, as does a Spanish entry that drifts from English.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from datamind_console.i18n import en, es, language_label, status_label, t

EN, ES = en.STRINGS, es.STRINGS
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
KEY_CALLERS = {"t": 0, "empty_state": 0, "loading": 0}
KEY_KWARGS = {"hint_key", "context_key", "message_key", "title_key"}


def _code_keys() -> set[str]:
    keys: set[str] = set()
    for path in Path("datamind_console").rglob("*.py"):
        if "/tests/" in str(path) or path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else None)
            if name in KEY_CALLERS and node.args:
                arg = node.args[KEY_CALLERS[name]]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    keys.add(arg.value)
            for kw in node.keywords:
                if kw.arg in KEY_KWARGS and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    keys.add(kw.value.value)
            if name in ("P", "PageSpec") and len(node.args) >= 5:
                for arg in (node.args[2], node.args[4]):
                    if isinstance(arg, ast.Constant):
                        keys.add(arg.value)
    return keys


def test_catalogs_have_identical_keys() -> None:
    assert set(EN) - set(ES) == set(), f"missing in es: {sorted(set(EN) - set(ES))}"
    assert set(ES) - set(EN) == set(), f"missing in en: {sorted(set(ES) - set(EN))}"


def test_every_key_used_in_code_exists() -> None:
    missing = sorted(k for k in _code_keys() if k not in EN)
    assert not missing, f"keys used in code but missing from the catalog: {missing}"


def test_placeholders_match_between_languages() -> None:
    mismatched = {
        key: (sorted(PLACEHOLDER.findall(EN[key])), sorted(PLACEHOLDER.findall(ES[key])))
        for key in EN
        if sorted(PLACEHOLDER.findall(EN[key])) != sorted(PLACEHOLDER.findall(ES.get(key, "")))
    }
    assert not mismatched, f"placeholder mismatch: {mismatched}"


def _has_prose(text: str) -> bool:
    """True when the text has words to translate, not only identifiers, code and placeholders."""
    text = re.sub(r"`[^`]*`|\{[a-z_0-9]+\}", " ", text)
    for token in text.split():
        word = token.strip("*-|:;,.()[]#=<>/'\"")
        if len(word) >= 3 and word.isalpha():
            return True
    return False


def test_spanish_is_actually_translated() -> None:
    same_ok = {"app.name", "nav.geo_api", "page.geo_api.title", "status.data_mode.local", "col.km",
               "col.seq", "status.generic.__none__", "phase.5.label", "term.alias", "settings.roles",
               "page.phases.eyebrow", "audit.catalog.missing",
               "ui.misc.geo_api_fusion.datamind_geo_api"}  # product names
    untranslated = sorted(k for k in EN if len(EN[k]) > 10 and EN[k] == ES[k] and k not in same_ok and _has_prose(EN[k]))
    assert not untranslated, f"Spanish equals English for: {untranslated}"


def test_spanish_keeps_its_accents() -> None:
    # The old monkey-patch catalog stripped accents ("Navegacion", "Ingles").
    banned = ("Navegacion", "Configuracion", "Contrasena", "Espanol", "Ingles", "Revision ", "sesion")
    offenders = {k: v for k, v in ES.items() for word in banned if word in v}
    assert not offenders, offenders
    assert language_label("es") == "Español" and language_label("en") == "English"


def test_dynamic_key_families_are_complete() -> None:
    from datamind_console.metrics.routes import ROUTE_METRIC_KEYS
    from datamind_console.ui.components.status import STATUS_TONES
    from datamind_console.views.phases.phase3_view import PHASE3_TAB_IDS

    required: list[str] = []
    for n in range(1, 6):
        required += [f"phase.{n}.label", f"phase.{n}.short", f"phase.{n}.description"]
    for tab in PHASE3_TAB_IDS:
        required += [f"phase3.tab.{tab}", f"phase3.tab.{tab}.description"]
    for key in ROUTE_METRIC_KEYS:
        required += [f"metric.{key}.label", f"metric.{key}.help"]
    for domain, values in STATUS_TONES.items():
        if domain in {"freshness", "dirty_reason"}:
            continue
        required += [f"status.{domain}.{v}" for v in values if v != "__default__"]
    required += [f"activity.{k}" for k in ("review_approved", "review_rejected", "reentry_swapped", "reentry_failed",
                                            "reentry_v2_ready", "feed_published", "synthetic_stops_created", "extraction_runs")]
    required += [f"review.reentry_outcome.{k}" for k in ("ready", "needs_phase2", "no_playbook", "invalid_current_shape", "other_error", "no_improvement", "swapped")]
    required += [f"review.reentry_next.{k}" for k in ("ready", "needs_phase2", "no_playbook", "invalid_current_shape", "other_error", "no_improvement", "swapped")]
    required += [f"review.quality_meaning.{k}" for k in ("good", "acceptable", "ship_pending_dr", "degraded_minor", "degraded", "unroutable", "__none__")]
    required += [f"audit.section.{k}" for k in ("inventory", "routes", "stops", "cohort")]
    required += [f"audit.deploy_scope.{k}" for k in ("all", "deployed", "not_deployed")]
    required += [f"audit.blocker.{k}" for k in ("stop_quality_pending", "stop_duplicate_residual", "canon_gap", "unprocessed", "unrealistic_runtime")]
    required += [f"audit.stop_flag.{k}" for k in ("empty", "placeholder", "predicted")]
    required += [f"gtfs.layer.{k}" for k in ("structure", "quality", "canonical")]
    required += [f"gtfs.layer_state.{k}" for k in ("pass", "fail", "not_run", "not_applicable")]
    required += [f"phase5.step.{k}" for k in ("00", "01", "02", "02b", "03", "04", "06", "07")]
    required += [f"overview.freshness.{k}" for k in ("fresh", "dormant", "last_extraction", "last_node_update",
                                                     "last_place_update", "last_review_activity", "last_reentry_activity", "last_feed_published")]
    required += [f"phase3.map.mode.{k}" for k in ("after_step_30", "before_step_30")]
    missing = sorted(k for k in required if k not in EN)
    assert not missing, f"dynamic keys missing: {missing}"


def test_translation_lookup_behaviour() -> None:
    assert t("nav.overview", lang="es") == "Resumen"
    assert t("time.days_ago", lang="en", n=3) == "3 days ago"
    assert t("does.not.exist", lang="es") == "does.not.exist"
    assert status_label("cleanliness", None, lang="es") == "Sin clasificar"
    assert status_label("cleanliness", "a_new_state", lang="en") == "A new state"


@pytest.mark.parametrize("key", ["metric.route_directions_clean.help", "gtfs.publish_ready.no", "phase5.step06.scope"])
def test_validation_and_metric_copy_is_explicit(key: str) -> None:
    # Guards against vague copy creeping back into the most safety-relevant text.
    assert len(EN[key]) > 60 and len(ES[key]) > 60
