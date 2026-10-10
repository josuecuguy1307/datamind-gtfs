"""Canonical route metrics.

Route counts used to be redefined page by page. The Sample Region audit showed
646 (a hard-coded caption from 2026-05-02, before the 2026-05-04 purge), 407
(active rows) and 458 (rows in every deploy state) side by side, all called
"routes". Every route number in the console now comes from here, with one
precise definition. See ``docs/METRICS.md``.

Vocabulary (see ``docs/TERMINOLOGY.md``):

* **route direction**: one row of ``route_prod.routes``. A direction (0/1)
  of a line, with its own geometry and stop sequence.
* **line**: a logical service route (``service_route_id``) grouping its
  directions; it corresponds to a GTFS ``routes.txt`` entry.
* **deployed** (active): ``deploy_status = 'active'``. Other states
  (``excluded_quality``, ``quarantined_no_stops``, ``non_sample_region``) are
  kept in production tables but are not deployed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from datamind_console.db.db import query_all, query_one

QueryOne = Callable[[str, Optional[Sequence]], Optional[dict]]
QueryAll = Callable[[str, Optional[Sequence]], list]


@dataclass(frozen=True)
class MetricDef:
    key: str
    label_key: str
    help_key: str


def _m(key: str) -> MetricDef:
    return MetricDef(key=key, label_key=f"metric.{key}.label", help_key=f"metric.{key}.help")


# Order matters: it is the display order used by KPI rows.
ROUTE_METRICS: tuple[MetricDef, ...] = (
    _m("route_directions_total"),
    _m("route_directions_deployed"),
    _m("route_directions_clean"),
    _m("route_directions_ship_eligible"),
    _m("route_directions_needs_work"),
    _m("route_directions_unclassified"),
    _m("route_directions_not_deployed"),
    _m("lines_deployed"),
    _m("lines_clean"),
    _m("route_directions_human_verified"),
)
ROUTE_METRIC_KEYS = tuple(m.key for m in ROUTE_METRICS)

# Single source of truth for every route count. ``%(province)s`` is NULL
# for "all provinces".
ROUTE_COUNTS_SQL = """
SELECT
  count(*)                                                                  AS route_directions_total,
  count(*) FILTER (WHERE deploy_status = 'active')                          AS route_directions_deployed,
  count(*) FILTER (WHERE deploy_status = 'active'
                     AND cleanliness_status = 'clean')                      AS route_directions_clean,
  count(*) FILTER (WHERE deploy_status = 'active'
                     AND (cleanliness_status = 'clean'
                          OR dirty_reason = 'phase4_catalog_synthesized'))  AS route_directions_ship_eligible,
  count(*) FILTER (WHERE deploy_status = 'active'
                     AND cleanliness_status = 'dirty')                      AS route_directions_needs_work,
  count(*) FILTER (WHERE deploy_status = 'active'
                     AND cleanliness_status IS NULL)                        AS route_directions_unclassified,
  count(*) FILTER (WHERE deploy_status IS DISTINCT FROM 'active')           AS route_directions_not_deployed,
  count(DISTINCT service_route_id) FILTER (WHERE deploy_status = 'active')  AS lines_deployed,
  count(DISTINCT service_route_id) FILTER (WHERE deploy_status = 'active'
                     AND cleanliness_status = 'clean')                      AS lines_clean,
  count(*) FILTER (WHERE human_verified)                                    AS route_directions_human_verified
FROM route_prod.routes
WHERE (%(province)s::text IS NULL OR province = %(province)s::text)
"""

DEPLOY_STATE_BREAKDOWN_SQL = """
SELECT COALESCE(deploy_status, 'unknown') AS deploy_status,
       COALESCE(cleanliness_status, 'unclassified') AS cleanliness_status,
       COALESCE(dirty_reason, '') AS dirty_reason,
       count(*) AS route_directions,
       count(DISTINCT service_route_id) AS lines
FROM route_prod.routes
WHERE (%(province)s::text IS NULL OR province = %(province)s::text)
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3
"""

PROVINCES_SQL = """
SELECT COALESCE(province, 'unknown') AS province, count(*) AS route_directions
FROM route_prod.routes
GROUP BY 1
ORDER BY 2 DESC
"""


def route_counts(province: Optional[str] = None, *, query: QueryOne = query_one) -> dict[str, int]:
    """Every canonical route count for one province (or all provinces)."""
    row = query(ROUTE_COUNTS_SQL, {"province": province}) or {}
    return {key: int(row.get(key) or 0) for key in ROUTE_METRIC_KEYS}


def deploy_state_breakdown(province: Optional[str] = None, *, query: QueryAll = query_all) -> list[dict]:
    """Route directions and lines per deploy state × cleanliness × reason."""
    return [dict(r) for r in query(DEPLOY_STATE_BREAKDOWN_SQL, {"province": province})]


def provinces(*, query: QueryAll = query_all) -> list[dict]:
    return [dict(r) for r in query(PROVINCES_SQL, None)]
