"""Localized formatting for numbers, dates and ages (EN/ES)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from datamind_console.i18n import get_lang, t

_MONTHS = {
    "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
    "es": ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"),
}


def format_number(value: Any, *, decimals: int = 0, lang: Optional[str] = None) -> str:
    """1234567 → '1,234,567' (EN) / '1.234.567' (ES). Non-numbers pass through."""
    if value is None:
        return "—"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    text = f"{value:,.{decimals}f}"
    if (lang or get_lang()) == "es":
        text = text.replace(",", " ").replace(".", ",").replace(" ", ".")
    return text


def _as_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def format_date(value: Any, *, with_time: bool = False, lang: Optional[str] = None) -> str:
    """'6 May 2026' (EN) / '6 may 2026' (ES); optional 24h time."""
    dt = _as_datetime(value)
    if dt is None:
        return "—"
    code = lang or get_lang()
    month = _MONTHS.get(code, _MONTHS["en"])[dt.month - 1]
    text = f"{dt.day} {month} {dt.year}"
    if with_time:
        text += f" · {dt.hour:02d}:{dt.minute:02d}"
    return text


def days_since(value: Any, *, now: Optional[datetime] = None) -> Optional[int]:
    dt = _as_datetime(value)
    if dt is None:
        return None
    ref = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, (ref - dt).days)


def format_age(value: Any, *, now: Optional[datetime] = None) -> str:
    """'today', '3 days ago', '5 months ago' (translated)."""
    days = days_since(value, now=now)
    if days is None:
        return "—"
    if days == 0:
        return t("time.today")
    if days < 60:
        return t("time.days_ago", n=days)
    return t("time.months_ago", n=round(days / 30.4))
