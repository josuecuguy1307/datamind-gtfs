"""Which stops may appear in an exported GTFS feed.

A synthetic stop a person rejected in the console (Review → Synthetic stops,
``node_prod.nodes.synthetic_review_state = 'rejected'``) stays in the
production tables for the audit trail but is left out of every export:
``stops.txt``, ``stop_times.txt`` and trip headsigns. Its route keeps running
through the remaining stops; when a leg-runtime binding no longer matches the
shorter sequence, stop_times falls back to uniform spacing (counted as
``leg_count_mismatch``). Pending and verified stops are exported as before.

Owner decision, 2026-10-09.
"""

# SQL condition on node_prod.nodes aliased as ``n``.
EXPORTABLE_STOP = "n.synthetic_review_state IS DISTINCT FROM 'rejected'"
