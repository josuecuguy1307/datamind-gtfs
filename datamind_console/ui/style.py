"""The one place where the console injects CSS.

Widgets are themed natively by ``.streamlit/config.toml``. This stylesheet
only covers spacing and the few DataMind elements Streamlit has no primitive
for (eyebrow labels, page header rule, muted captions). Pages must not add
their own ``<style>`` blocks; add a class here instead.
"""
from __future__ import annotations

import json

import streamlit as st

from datamind_console.i18n import t
from datamind_console.ui.theme import RADIUS, TYPE_SCALE, palette

_CSS = """
<style>
  /* Layout rhythm */
  .block-container {{ padding-top: 4.25rem; padding-bottom: 3rem; max-width: 1480px; }}
  section[data-testid="stSidebar"] .block-container {{ padding-top: 1rem; }}

  /* DataMind header elements */
  .dm-eyebrow {{
    font-size: {eyebrow_size};
    font-weight: 650;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: {accent_text};
    margin: 0 0 0.15rem 0;
  }}
  .dm-subtitle {{ color: {text_muted}; font-size: 0.98rem; margin: -0.35rem 0 0.35rem 0; max-width: 72ch; }}
  .dm-rule {{ height: 3px; width: 56px; border-radius: {radius_pill}; background: {accent}; margin: 0.2rem 0 1.1rem 0; }}
  .dm-muted {{ color: {text_muted}; }}
  .dm-kicker {{ color: {text_muted}; font-size: {caption_size}; letter-spacing: 0.04em; text-transform: uppercase; font-weight: 600; }}

  /* Metric cards: steadier label/number rhythm */
  [data-testid="stMetric"] [data-testid="stMetricLabel"] p {{ font-size: 0.82rem; letter-spacing: 0.02em; color: {text_muted}; }}
  /* Labels wrap instead of truncating ("Route directions de…") */
  [data-testid="stMetricLabel"], [data-testid="stMetricLabel"] * {{
    white-space: normal !important; overflow: visible !important; text-overflow: clip !important;
  }}
  [data-testid="stMetric"] [data-testid="stMetricValue"] {{ font-variant-numeric: tabular-nums; }}

  /* Tables and code: tabular numerals */
  [data-testid="stDataFrame"] {{ font-variant-numeric: tabular-nums; }}
</style>
"""


# st.file_uploader's dropzone text is hard-coded English inside Streamlit and
# has no parameter. The original text is hidden (font-size 0) and the catalog
# text is drawn in its place, keeping the accepted file type per uploader.
_UPLOADER = '[data-testid="stFileUploaderDropzone"]'
_UPLOADER_TYPES = {".zip": "ZIP", ".json": "JSON"}


def _css_string(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def _uploader_css() -> str:
    limit = t("ui.uploader.limit", size=st.get_option("server.maxUploadSize"))
    instructions = f"{_UPLOADER} [data-testid=\"stFileUploaderDropzoneInstructions\"] > div > span"
    rules = [
        f"{instructions} {{ font-size: 0; }}",
        f"{instructions}:first-child::after {{ content: {_css_string(t('ui.uploader.drop'))}; font-size: 1rem; }}",
        f"{instructions}:last-child::after {{ content: {_css_string(limit)}; font-size: 0.875rem; }}",
        f"{_UPLOADER} button {{ font-size: 0; }}",
        f"{_UPLOADER} button::after {{ content: {_css_string(t('ui.uploader.browse'))}; font-size: 1rem; }}",
    ]
    for ext, label in _UPLOADER_TYPES.items():
        rules.append(
            f'{_UPLOADER}:has(input[accept*="{ext}"]) [data-testid="stFileUploaderDropzoneInstructions"] > div > span:last-child::after '
            f"{{ content: {_css_string(f'{limit} • {label}')}; }}"
        )
    return "<style>\n" + "\n".join(rules) + "\n</style>"


def apply_global_style() -> None:
    p = palette()
    st.html(
        _CSS.format(
            eyebrow_size=TYPE_SCALE["eyebrow"],
            caption_size=TYPE_SCALE["caption"],
            accent=p.accent,
            accent_text=p.accent_text,
            text_muted=p.text_muted,
            radius_pill=RADIUS["pill"],
        )
        + _uploader_css()
    )
