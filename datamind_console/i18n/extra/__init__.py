"""Additional catalogs, one module per surface, English and Spanish side by side.

Each module defines ``EN`` and ``ES`` dicts with identical keys. ``en.py`` and
``es.py`` merge them into their ``STRINGS``, so ``t()`` and the catalog tests
see one catalog per language. Keeping a surface's two languages in one file
makes drift visible in review.
"""
from __future__ import annotations

import importlib
import pkgutil

EN: dict[str, str] = {}
ES: dict[str, str] = {}

for _info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
    _mod = importlib.import_module(f"{__name__}.{_info.name}")
    _en, _es = getattr(_mod, "EN", {}), getattr(_mod, "ES", {})
    _dupes = (set(_en) & set(EN)) | (set(_es) & set(ES))
    if _dupes:
        raise ValueError(f"duplicate i18n keys in {_info.name}: {sorted(_dupes)[:5]}")
    EN.update(_en)
    ES.update(_es)
