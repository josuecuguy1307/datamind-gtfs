# Phase 3 Route Trash / Papelera Architecture

## Overview

Routes removed by cleanup, merge, dedupe, or manual deletion are **not permanently lost**.
They are first snapshotted to a trash/archive system, then deactivated from active surfaces.

**Flow: active route → trash snapshot → soft-delete flag → optional later restore**

## Schema

All trash objects live in the `route_trash` schema:

### `route_trash.trash_items`
Stores full JSONB snapshots of trashed route-level objects.

Key columns:
- `trash_id` (PK) — unique identifier for the trash entry
- `route_id` — the original route
- `full_snapshot_jsonb` — complete snapshot of route_jobs, relation_candidates, sequence/geometry approvals, prod route, dedupe memberships, etc.
- `deletion_reason`, `deletion_source_workflow`, `deleted_by`, `deleted_at`
- `replaced_by_route_id` — if route was merged/replaced
- `restore_status` — `trashed` | `restored` | `purged`

### `route_trash.delete_events`
Audit log of every deletion/cleanup action.

Key columns:
- `action_type` — `delete` | `deactivate` | `merge` | `replace` | `cleanup` | `purge` | `restore` | `suppress`
- `workflow_source`, `reason`, `actor`, `event_at`
- `trash_id` — link to the snapshot
- `replacement_route_id`, `canonical_route_id` — for merge/dedupe tracking

### Soft-delete on `route_raw.route_jobs`
- `is_trashed` (BOOLEAN, default FALSE) — marks route as trashed
- `trashed_at` — when it was trashed
- `trash_id` — FK to trash_items

### `route_raw.active_route_jobs` (VIEW)
Drop-in replacement: `SELECT * FROM route_raw.route_jobs WHERE is_trashed = FALSE`

## Operational Behavior

- Trashing a route now snapshots route-level raw/work/prod state before cleanup.
- Active cleanup removes the route from `route_prod.routes`, invalidates sequence approval, clears route approvals, deletes Phase 3 node review requests, and unbinds the route from `service_route_directions`.
- Review/catalog surfaces default to `route_raw.active_route_jobs`, so trashed routes no longer appear in the active queue/catalog by default.
- Restore brings the route back to active state with `status='new'` (or the requested restore status) and attempts to rebind the original `service_route_id` / `direction_id` slot if it is safe to do so.

## Usage

### Python API (trash_repo)

```python
from phase3_routes.services.route_constructor.src.db.trash_repo import (
    trash_route, restore_route, trash_route_for_merge,
    get_trash_item, list_trash, list_delete_events, is_route_trashed,
)

# Trash a route
trash_id = trash_route(conn, route_id, reason="duplicate", workflow="dedupe_cleanup", actor="operator")

# Restore it
restore_route(conn, trash_id, actor="operator", restore_status="new")

# Merge-trash (records replacement link)
trash_route_for_merge(conn, route_id, canonical_route_id, workflow="dedupe_merge")
```

### Console Client API

```python
client = Phase3Client()

# Trash
result = client.trash_route_job(route_id, reason="duplicate", workflow="dedupe_cleanup")
# → {"route_id": "...", "trash_id": "...", "status": "trashed"}

# Restore
result = client.restore_route(trash_id)
# → {"route_id": "...", "trash_id": "...", "status": "restored"}

# List trash
items = client.list_trash(workflow="dedupe_cleanup")

# Audit events
events = client.list_delete_events(route_id=some_id)
```

### delete_route_job Behavior

`delete_route_job()` now defaults to **trash/archive behavior**, not hard deletion.

- Default: snapshot to trash, deactivate active route surfaces, keep the base route record recoverable.
- Purge only when explicitly requested (`purge=True` or legacy `skip_trash=True`).

## Supported Action Types

| Action | Use case |
|--------|----------|
| `delete` | Route removed from active surface |
| `merge` | Route merged into canonical route |
| `replace` | Route replaced by another |
| `cleanup` | Bulk/automated cleanup |
| `deactivate` | Soft-deactivation |
| `suppress` | Dedupe suppression |
| `restore` | Route restored from trash |
| `purge` | Permanent removal (future) |

## Restore Behavior

- Restores route_jobs.is_trashed = FALSE and resets status
- Recreates / rebinds the logical `service_route_id` direction slot when possible
- Marks trash item as `restored` with timestamp and actor
- Logs a `restore` audit event
- Idempotent: restoring an already-restored item is a no-op
- Does not automatically republish prod geometry/approvals; restored routes return to review/pending state by design

## Migration

Apply: `phase3_routes/services/route_constructor/sql/030_route_trash.sql`

## Tests

Run: `python -m pytest phase3_routes/services/route_constructor/tests/test_trash_repo.py -v`
