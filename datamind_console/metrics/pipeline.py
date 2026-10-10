"""Pipeline state for the operational home and the review overview.

Read-only queries with precise definitions (see ``docs/METRICS.md``). Nothing
here is estimated or inferred: every number is a COUNT or MAX over a named
table and condition.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from datamind_console.common.env import PROJECT_ROOT
from datamind_console.db.db import query_all, query_one

QueryOne = Callable[[str, Optional[Sequence]], Optional[dict]]
QueryAll = Callable[[str, Optional[Sequence]], list]

FRESHNESS_SQL = """
SELECT
  (SELECT max(fetched_at)   FROM node_raw.overpass_runs)      AS last_extraction,
  (SELECT max(updated_at)   FROM node_prod.nodes)             AS last_node_update,
  (SELECT max(updated_at)   FROM geo_prod.places)             AS last_place_update,
  (SELECT max(GREATEST(enqueued_at, COALESCE(resolved_at, enqueued_at)))
     FROM route_prod.approval_queue)                          AS last_review_activity,
  (SELECT max(GREATEST(enqueued_at, COALESCE(resolved_at, enqueued_at)))
     FROM route_prod.re_entry_queue)                          AS last_reentry_activity,
  (SELECT max(published_at) FROM gtfs_prod.feed_versions)     AS last_feed_published
"""

ATTENTION_SQL = """
SELECT
  (SELECT count(*) FROM node_prod.nodes
    WHERE synthetic_review_state = 'pending')                               AS synthetic_stops_pending,
  (SELECT count(*) FROM route_prod.approval_queue WHERE status = 'pending') AS approval_pending,
  (SELECT count(*) FROM route_prod.approval_queue
    WHERE status = 'pending' AND COALESCE(crashed, false))                  AS approval_crashed,
  (SELECT count(*) FROM route_prod.re_entry_queue WHERE status = 'v2_ready') AS reentry_ready,
  (SELECT count(*) FROM route_prod.re_entry_queue
    WHERE status = 'failed'
      AND COALESCE(last_error, '') NOT LIKE 'no_improvement%%')                AS reentry_blocked,
  (SELECT count(*) FROM route_prod.re_entry_queue
    WHERE status = 'failed'
      AND COALESCE(last_error, '') LIKE 'no_improvement%%')                    AS reentry_no_improvement,
  (SELECT count(*) FROM route_prod.routes
    WHERE deploy_status = 'active' AND naming_confidence IS NULL)           AS deployed_without_naming_confidence,
  (SELECT count(*) FROM route_prod.routes
    WHERE deploy_status = 'active' AND naming_confidence < 0.5)             AS deployed_low_naming_confidence,
  (SELECT count(*) FROM gtfs_prod.feed_versions WHERE is_current)           AS current_feeds
"""

APPROVAL_PENDING_BY_QUALITY_SQL = """
SELECT COALESCE(quality_class, '') AS quality_class, count(*) AS n
FROM route_prod.approval_queue
WHERE status = 'pending'
GROUP BY 1
ORDER BY 2 DESC
"""

APPROVAL_STATUS_SQL = """
SELECT status, count(*) AS n
FROM route_prod.approval_queue
GROUP BY 1
ORDER BY 2 DESC
"""

REENTRY_STATUS_SQL = """
SELECT status, count(*) AS n
FROM route_prod.re_entry_queue
GROUP BY 1
ORDER BY 2 DESC
"""

CURRENT_FEED_SQL = """
SELECT feed_version_id::text AS feed_version_id,
       published_at,
       is_current,
       gtfs_zip_path,
       (validator_report IS NOT NULL) AS has_validator_report
FROM gtfs_prod.feed_versions
ORDER BY is_current DESC, published_at DESC NULLS LAST
LIMIT 1
"""

# Recent activity: real events, grouped by day and kind (bulk runs would
# otherwise flood the list), newest first. No person names are selected.
RECENT_ACTIVITY_SQL = """
WITH events AS (
  SELECT aq.resolved_at AS at, 3 AS phase,
         'review_' || aq.status AS kind,
         COALESCE(r.route_name, '#' || left(aq.route_code, 8)) AS subject
  FROM route_prod.approval_queue aq
  LEFT JOIN route_prod.routes r ON r.route_id::text = aq.route_code
  WHERE aq.resolved_at IS NOT NULL
  UNION ALL
  SELECT q.resolved_at, 3, 'reentry_' || q.status, COALESCE(r.route_name, '#' || left(q.route_id::text, 8))
  FROM route_prod.re_entry_queue q
  LEFT JOIN route_prod.routes r ON r.route_id = q.route_id
  WHERE q.resolved_at IS NOT NULL
  UNION ALL
  SELECT published_at, 5, 'feed_published', COALESCE(gtfs_zip_path, feed_version_id::text)
  FROM gtfs_prod.feed_versions
  WHERE published_at IS NOT NULL
  UNION ALL
  SELECT synthetic_created_at, 1, 'synthetic_stops_created', COALESCE(name, node_id::text)
  FROM node_prod.nodes
  WHERE synthetic_created_at IS NOT NULL
  UNION ALL
  SELECT fetched_at, 1, 'extraction_runs', COALESCE(area_id, run_id::text)
  FROM node_raw.overpass_runs
  WHERE fetched_at IS NOT NULL
)
SELECT date_trunc('day', at)::date AS day,
       phase,
       kind,
       count(*) AS n,
       (array_agg(subject ORDER BY at DESC))[1:3] AS examples,
       max(at) AS last_at
FROM events
GROUP BY 1, 2, 3
ORDER BY last_at DESC
LIMIT %(limit)s
"""


# Re-entry outcomes. status='failed' mixes real blockers with attempts where
# the worker built a v2, found no improvement and kept the current version.
REENTRY_OUTCOMES_SQL = """
SELECT
  count(*) FILTER (WHERE status = 'v2_ready')                                         AS ready,
  count(*) FILTER (WHERE status = 'swapped')                                          AS swapped,
  count(*) FILTER (WHERE status = 'failed' AND COALESCE(last_error, '') LIKE 'no_improvement%%')            AS no_improvement,
  count(*) FILTER (WHERE status = 'failed' AND COALESCE(last_error, '') LIKE 'coordinator rejected%%')      AS needs_phase2,
  count(*) FILTER (WHERE status = 'failed' AND COALESCE(last_error, '') LIKE 'classification=unclassified%%') AS no_playbook,
  count(*) FILTER (WHERE status = 'failed' AND COALESCE(last_error, '') LIKE 'v1 shape too small%%')        AS invalid_current_shape,
  count(*) FILTER (WHERE status = 'failed'
                     AND COALESCE(last_error, '') NOT LIKE 'no_improvement%%'
                     AND COALESCE(last_error, '') NOT LIKE 'coordinator rejected%%'
                     AND COALESCE(last_error, '') NOT LIKE 'classification=unclassified%%'
                     AND COALESCE(last_error, '') NOT LIKE 'v1 shape too small%%')                          AS other_error,
  count(*) FILTER (WHERE status NOT IN ('v2_ready', 'swapped', 'failed'))           AS other_status
FROM route_prod.re_entry_queue
"""

APPROVAL_PENDING_SPLIT_SQL = """
SELECT
  count(*) FILTER (WHERE COALESCE(policy_flags, '{}'::jsonb) ? 're_entry_queue_id')       AS reentry_proposals,
  count(*) FILTER (WHERE NOT (COALESCE(policy_flags, '{}'::jsonb) ? 're_entry_queue_id')) AS other_proposals
FROM route_prod.approval_queue
WHERE status = 'pending'
"""

COVERAGE_GAPS_SQL = """
SELECT COALESCE(resolution_status, 'unknown') AS resolution_status, count(*) AS n
FROM route_review.coverage_gaps
GROUP BY 1
ORDER BY 2 DESC
"""

SYNTHETIC_STOPS_SQL = """
SELECT COALESCE(source_type, 'unknown') AS source_type,
       COALESCE(synthetic_review_state, 'unknown') AS review_state,
       count(*) AS n,
       min(synthetic_created_at) AS first_created,
       max(synthetic_created_at) AS last_created
FROM node_prod.nodes
WHERE synthetic_review_state IS NOT NULL
GROUP BY 1, 2
ORDER BY 3 DESC
"""

RECENT_DECISIONS_SQL = """
SELECT phase, decision, reason_code, item_id, created_at
FROM console.phase_decisions
ORDER BY created_at DESC
LIMIT %(limit)s
"""


def reentry_outcomes(*, query: QueryOne = query_one) -> dict[str, int]:
    row = query(REENTRY_OUTCOMES_SQL, None) or {}
    return {k: int(v or 0) for k, v in row.items()}


def approval_pending_split(*, query: QueryOne = query_one) -> dict[str, int]:
    row = query(APPROVAL_PENDING_SPLIT_SQL, None) or {}
    return {k: int(v or 0) for k, v in row.items()}


def coverage_gaps(*, query: QueryAll = query_all) -> dict[str, int]:
    return {str(r["resolution_status"]): int(r["n"]) for r in query(COVERAGE_GAPS_SQL, None)}


def synthetic_stops(*, query: QueryAll = query_all) -> list[dict]:
    return [dict(r) for r in query(SYNTHETIC_STOPS_SQL, None)]


def recent_decisions(limit: int = 20, *, query: QueryAll = query_all) -> list[dict]:
    return [dict(r) for r in query(RECENT_DECISIONS_SQL, {"limit": int(limit)})]


def freshness(*, query: QueryOne = query_one) -> dict[str, Any]:
    return dict(query(FRESHNESS_SQL, None) or {})


def attention(*, query: QueryOne = query_one) -> dict[str, int]:
    row = query(ATTENTION_SQL, None) or {}
    return {k: int(v or 0) for k, v in row.items()}


def approval_pending_by_quality(*, query: QueryAll = query_all) -> list[dict]:
    return [dict(r) for r in query(APPROVAL_PENDING_BY_QUALITY_SQL, None)]


def approval_status(*, query: QueryAll = query_all) -> dict[str, int]:
    return {str(r["status"]): int(r["n"]) for r in query(APPROVAL_STATUS_SQL, None)}


def reentry_status(*, query: QueryAll = query_all) -> dict[str, int]:
    return {str(r["status"]): int(r["n"]) for r in query(REENTRY_STATUS_SQL, None)}


def current_feed(*, query: QueryOne = query_one) -> dict[str, Any]:
    """Current feed version, plus ``zip_exists`` (None when no path is recorded).

    On 2026-10-08 the registered current feed pointed at a ZIP that was no
    longer on disk; the Overview flags that instead of implying it is fine.
    """
    feed = dict(query(CURRENT_FEED_SQL, None) or {})
    raw = feed.get("gtfs_zip_path")
    if raw:
        path = Path(str(raw)).expanduser()
        feed["zip_exists"] = (path if path.is_absolute() else PROJECT_ROOT / path).is_file()
    else:
        feed["zip_exists"] = None
    return feed


def recent_activity(limit: int = 12, *, query: QueryAll = query_all) -> list[dict]:
    return [dict(r) for r in query(RECENT_ACTIVITY_SQL, {"limit": int(limit)})]
