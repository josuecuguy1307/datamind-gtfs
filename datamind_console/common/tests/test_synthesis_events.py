"""
Unit tests for datamind_console.common.synthesis_events.

We use an in-memory fake connection + cursor to avoid a DB dependency in Phase
1.  The fake mirrors the bits of psycopg2's surface that the module relies on:
- context-manager cursors
- parameterised ``execute()``
- ``fetchone()``, ``fetchall()``, ``rowcount``

The integration test (``test_phase1_integration.py``) exercises the real DB.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from datamind_console.common import synthesis_events as se


# ---------------------------------------------------------------------------
# Minimal in-memory psycopg2-ish fake
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, store):
        self._store = store
        self.rowcount = 0
        self._result: list[dict] = []
        self.last_sql: str | None = None
        self.last_params: tuple | None = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        params = tuple(params or ())
        self.last_sql = sql
        self.last_params = params
        sql_norm = " ".join(sql.split())
        sql_upper = sql_norm.upper()

        if sql_upper.startswith("INSERT INTO NODE_PROD.SYNTHESIS_EVENTS"):
            row = self._store._insert(params)
            self._result = [row]
            self.rowcount = 1
            return

        # GROUP BY STAGE must come BEFORE the generic COUNT(*) WHERE branch
        if sql_upper.startswith("SELECT") and "GROUP BY STAGE" in sql_upper:
            stage_counts: dict[str, int] = {}
            for r in self._store.rows:
                if r.get("rejected_reason"):
                    continue
                stage_counts[r["stage"]] = stage_counts.get(r["stage"], 0) + 1
            self._result = [
                {"stage": s, "n": c} for s, c in sorted(stage_counts.items())
            ]
            self.rowcount = len(self._result)
            return

        if "COUNT(*)" in sql_upper and "WHERE" in sql_upper:
            filters = self._store._parse_filters(sql_norm, params)
            count = self._store._count(filters)
            self._result = [{"n": count}]
            self.rowcount = 1
            return

        if sql_upper.startswith("SELECT") and "WHERE NODE_ID" in sql_upper:
            node_id = params[0]
            rows = [
                dict(r) for r in self._store.rows if r.get("node_id") == node_id
            ]
            self._result = sorted(
                rows, key=lambda r: r["created_at"], reverse=True
            )
            self.rowcount = len(self._result)
            return

        raise AssertionError(f"fake cursor got unexpected SQL: {sql!r}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class _FakeConn:
    def __init__(self, store):
        self._store = store
        self.committed = False
        self.rolled_back = False

    def cursor(self, *args, **kwargs):
        return _FakeCursor(self._store)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class _FakeStore:
    def __init__(self):
        self.rows: list[dict] = []
        self._seq = 0

    def _insert(self, params):
        self._seq += 1
        fields = [
            "node_id", "route_id", "unit", "province",
            "stage", "anchor_name",
            "research_coords_lat", "research_coords_lon",
            "final_coords_lat", "final_coords_lon",
            "match_score", "rejected_reason",
            "triggered_by_skill", "research_output_file",
        ]
        row: dict = {"id": self._seq, "created_at": datetime.now(timezone.utc)}
        for i, f in enumerate(fields):
            row[f] = params[i] if i < len(params) else None
        self.rows.append(row)
        return row

    def _parse_filters(self, sql, params):
        """Match %s placeholders to columns in left-to-right WHERE order."""
        filters: dict = {}
        sql_u = sql.upper()
        idx = 0
        # Walk placeholders in original order
        placeholder_positions = []
        cursor = 0
        while True:
            pos = sql.find("%s", cursor)
            if pos == -1:
                break
            placeholder_positions.append(pos)
            cursor = pos + 2
        # For each placeholder, figure out which WHERE clause it belongs to by
        # scanning backward for the nearest column-equals token.
        for i, pos in enumerate(placeholder_positions):
            prefix = sql_u[:pos]
            if "ROUTE_ID = " in prefix and prefix.rfind("ROUTE_ID = ") > max(
                prefix.rfind("STAGE ="), prefix.rfind("UNIT ="),
                prefix.rfind("CREATED_AT >="),
            ):
                filters["route_id"] = params[i]
            elif "STAGE = " in prefix and prefix.rfind("STAGE = ") > max(
                prefix.rfind("UNIT ="), prefix.rfind("CREATED_AT >="),
            ):
                filters["stage"] = params[i]
            elif "UNIT = " in prefix and prefix.rfind("UNIT = ") > prefix.rfind(
                "CREATED_AT >="
            ):
                filters["unit"] = params[i]
            elif "CREATED_AT >= " in prefix:
                filters["since"] = params[i]
        if "REJECTED_REASON IS NULL" in sql_u:
            filters["succeeded_only"] = True
        return filters

    def _count(self, filters):
        n = 0
        for r in self.rows:
            if filters.get("succeeded_only") and r.get("rejected_reason"):
                continue
            if "route_id" in filters and r.get("route_id") != filters["route_id"]:
                continue
            if "stage" in filters and r.get("stage") != filters["stage"]:
                continue
            if "stage_in" in filters and r.get("stage") not in filters["stage_in"]:
                continue
            if "unit" in filters and r.get("unit") != filters["unit"]:
                continue
            if "since" in filters and r.get("created_at") < filters["since"]:
                continue
            n += 1
        return n


@pytest.fixture()
def store() -> _FakeStore:
    return _FakeStore()


@pytest.fixture()
def conn(store) -> _FakeConn:
    return _FakeConn(store)


def _route_id() -> str:
    return str(uuid.uuid4())


def _node_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# 1. log_synthesis_event — happy path, returns inserted row
# ---------------------------------------------------------------------------


def test_log_synthesis_event_happy_path(conn, store):
    node_id = _node_id()
    route_id = _route_id()
    row = se.log_synthesis_event(
        conn,
        node_id=node_id,
        route_id=route_id,
        unit="ruminahui",
        province="sample_region",
        stage="3a_poi_on_path",
        anchor_name="Y de Calsig",
        final_coords=(-0.316, -78.443),
        match_score=0.92,
        triggered_by_skill="hades-path-aware-synthesis",
        research_output_file="01_stop_grounding_detail_ruminahui_CAL-03_20260419-1445.json",
    )
    assert len(store.rows) == 1
    stored = store.rows[0]
    assert stored["node_id"] == node_id
    assert stored["route_id"] == route_id
    assert stored["stage"] == "3a_poi_on_path"
    assert stored["unit"] == "ruminahui"
    assert row["id"] == 1


# ---------------------------------------------------------------------------
# 2. log_synthesis_event — rejection path (no node_id, rejected_reason set)
# ---------------------------------------------------------------------------


def test_log_synthesis_event_rejection(conn, store):
    route_id = _route_id()
    se.log_synthesis_event(
        conn,
        node_id=None,
        route_id=route_id,
        unit="ruminahui",
        province="sample_region",
        stage="4_pure_synthesis",
        rejected_reason="route_synthesis_cap_hit",
        triggered_by_skill="hades-path-aware-synthesis",
    )
    assert store.rows[0]["node_id"] is None
    assert store.rows[0]["rejected_reason"] == "route_synthesis_cap_hit"


# ---------------------------------------------------------------------------
# 3. invalid stage is rejected before hitting the DB
# ---------------------------------------------------------------------------


def test_log_synthesis_event_rejects_unknown_stage(conn, store):
    with pytest.raises(ValueError, match="stage"):
        se.log_synthesis_event(
            conn,
            node_id=None,
            route_id=_route_id(),
            unit="ruminahui",
            province="sample_region",
            stage="bogus_stage",
            triggered_by_skill="hades-path-aware-synthesis",
        )
    assert not store.rows


# ---------------------------------------------------------------------------
# 4. unit + province are required non-empty strings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["unit", "province"])
def test_log_synthesis_event_requires_unit_and_province(conn, store, missing):
    kwargs = dict(
        node_id=_node_id(),
        route_id=_route_id(),
        unit="ruminahui",
        province="sample_region",
        stage="3a_poi_on_path",
        triggered_by_skill="hades-path-aware-synthesis",
    )
    kwargs[missing] = ""
    with pytest.raises(ValueError, match=missing):
        se.log_synthesis_event(conn, **kwargs)
    assert not store.rows


# ---------------------------------------------------------------------------
# 5. coords tuple is unpacked correctly
# ---------------------------------------------------------------------------


def test_log_synthesis_event_coord_unpacking(conn, store):
    se.log_synthesis_event(
        conn,
        node_id=_node_id(),
        route_id=_route_id(),
        unit="u",
        province="p",
        stage="3d_research_coords_snapped",
        research_coords=(-0.30, -78.44),
        final_coords=(-0.31, -78.45),
        triggered_by_skill="hades-path-aware-synthesis",
    )
    r = store.rows[0]
    assert r["research_coords_lat"] == -0.30
    assert r["research_coords_lon"] == -78.44
    assert r["final_coords_lat"] == -0.31
    assert r["final_coords_lon"] == -78.45


def test_log_synthesis_event_rejects_malformed_coords(conn, store):
    with pytest.raises(ValueError, match="final_coords"):
        se.log_synthesis_event(
            conn,
            node_id=_node_id(),
            route_id=_route_id(),
            unit="u",
            province="p",
            stage="4_pure_synthesis",
            final_coords=(-0.31,),  # too short
            triggered_by_skill="hades-path-aware-synthesis",
        )
    assert not store.rows


# ---------------------------------------------------------------------------
# 6. all 6 valid stages accepted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stage",
    [
        "3a_poi_on_path",
        "3b_path_corridor",
        "3c_path_intersection",
        "3d_research_coords_snapped",
        "4_pure_synthesis",
        "osm_route_fill",
    ],
)
def test_log_synthesis_event_all_valid_stages(conn, store, stage):
    se.log_synthesis_event(
        conn,
        node_id=_node_id(),
        route_id=_route_id(),
        unit="u",
        province="p",
        stage=stage,
        triggered_by_skill="hades-path-aware-synthesis",
    )
    assert store.rows[-1]["stage"] == stage


# ---------------------------------------------------------------------------
# 7. module does NOT touch the free-text `source` string — callers do
# ---------------------------------------------------------------------------


def test_log_synthesis_event_has_no_source_parameter():
    import inspect
    sig = inspect.signature(se.log_synthesis_event)
    assert "source" not in sig.parameters
    assert "source_type" not in sig.parameters


# ---------------------------------------------------------------------------
# 8. count_synthesis_events_for_route
# ---------------------------------------------------------------------------


def test_count_synthesis_events_for_route(conn, store):
    route_a = _route_id()
    route_b = _route_id()
    for stage in ("3a_poi_on_path", "4_pure_synthesis", "3d_research_coords_snapped"):
        se.log_synthesis_event(
            conn, node_id=_node_id(), route_id=route_a, unit="u", province="p",
            stage=stage, triggered_by_skill="x",
        )
    se.log_synthesis_event(
        conn, node_id=_node_id(), route_id=route_b, unit="u", province="p",
        stage="4_pure_synthesis", triggered_by_skill="x",
    )
    assert se.count_synthesis_events_for_route(conn, route_id=route_a) == 3
    assert se.count_synthesis_events_for_route(conn, route_id=route_b) == 1


# ---------------------------------------------------------------------------
# 9. count_pure_synthesis_events_for_route filters rejections out
# ---------------------------------------------------------------------------


def test_count_pure_synthesis_events_excludes_rejections(conn, store):
    route_id = _route_id()
    se.log_synthesis_event(
        conn, node_id=_node_id(), route_id=route_id, unit="u", province="p",
        stage="4_pure_synthesis", triggered_by_skill="x",
    )
    se.log_synthesis_event(
        conn, node_id=_node_id(), route_id=route_id, unit="u", province="p",
        stage="4_pure_synthesis", triggered_by_skill="x",
    )
    se.log_synthesis_event(
        conn, node_id=None, route_id=route_id, unit="u", province="p",
        stage="4_pure_synthesis", rejected_reason="route_synthesis_cap_hit",
        triggered_by_skill="x",
    )
    assert se.count_pure_synthesis_events_for_route(conn, route_id=route_id) == 2


# ---------------------------------------------------------------------------
# 10. count_synthesis_events_for_unit_week window math
# ---------------------------------------------------------------------------


def test_count_synthesis_events_for_unit_week(conn, store):
    now = datetime.now(timezone.utc)
    for _ in range(4):
        se.log_synthesis_event(
            conn, node_id=_node_id(), route_id=_route_id(), unit="ruminahui",
            province="sample_region", stage="3a_poi_on_path",
            triggered_by_skill="x",
        )
    # tamper: make one row 8 days old
    store.rows[0]["created_at"] = now - timedelta(days=8)

    count = se.count_synthesis_events_for_unit_week(
        conn, unit="ruminahui", reference=now
    )
    assert count == 3


def test_count_synthesis_events_for_unit_week_requires_unit(conn):
    with pytest.raises(ValueError, match="unit"):
        se.count_synthesis_events_for_unit_week(conn, unit="", reference=None)


# ---------------------------------------------------------------------------
# 11. get_events_for_node returns newest-first list
# ---------------------------------------------------------------------------


def test_get_events_for_node_returns_newest_first(conn, store):
    node_id = _node_id()
    for stage in ("3a_poi_on_path", "4_pure_synthesis"):
        se.log_synthesis_event(
            conn, node_id=node_id, route_id=_route_id(), unit="u",
            province="p", stage=stage, triggered_by_skill="x",
        )
    # force the second insert to appear earlier
    store.rows[0]["created_at"] = datetime.now(timezone.utc) - timedelta(hours=1)
    store.rows[1]["created_at"] = datetime.now(timezone.utc)

    rows = se.get_events_for_node(conn, node_id=node_id)
    assert len(rows) == 2
    assert rows[0]["stage"] == "4_pure_synthesis"
    assert rows[1]["stage"] == "3a_poi_on_path"


# ---------------------------------------------------------------------------
# 12. get_calibration_stats — pure fraction math
# ---------------------------------------------------------------------------


def test_get_calibration_stats_pure_fraction(conn, store):
    for stage in (
        "3a_poi_on_path",
        "3a_poi_on_path",
        "3d_research_coords_snapped",
        "4_pure_synthesis",
    ):
        se.log_synthesis_event(
            conn, node_id=_node_id(), route_id=_route_id(), unit="u",
            province="p", stage=stage, triggered_by_skill="x",
        )
    stats = se.get_calibration_stats(conn)
    assert stats["total_succeeded"] == 4
    assert stats["by_stage"]["4_pure_synthesis"] == 1
    assert stats["by_stage"]["3a_poi_on_path"] == 2
    assert stats["pure_synthesis_fraction"] == pytest.approx(0.25)


def test_get_calibration_stats_empty(conn, store):
    stats = se.get_calibration_stats(conn)
    assert stats["total_succeeded"] == 0
    assert stats["pure_synthesis_fraction"] == 0.0
    assert stats["by_stage"] == {}


# ---------------------------------------------------------------------------
# 13. stage-weight helper — cap math lives here, spec §8
# ---------------------------------------------------------------------------


def test_stage_weight_values():
    assert se.stage_weight("3a_poi_on_path") == 0.0
    assert se.stage_weight("3b_path_corridor") == 0.0
    assert se.stage_weight("3c_path_intersection") == 0.0
    assert se.stage_weight("3d_research_coords_snapped") == 0.5
    assert se.stage_weight("4_pure_synthesis") == 1.0
    assert se.stage_weight("osm_route_fill") == 0.0


def test_stage_weight_rejects_unknown():
    with pytest.raises(ValueError):
        se.stage_weight("bogus_stage")


# ---------------------------------------------------------------------------
# 14. compute_route_cap_consumed sums weights, respects rejections
# ---------------------------------------------------------------------------


def test_compute_route_cap_consumed_sums_weights(conn, store):
    route_id = _route_id()
    for stage in ("3a_poi_on_path", "3d_research_coords_snapped", "4_pure_synthesis"):
        se.log_synthesis_event(
            conn, node_id=_node_id(), route_id=route_id, unit="u", province="p",
            stage=stage, triggered_by_skill="x",
        )
    # 0.0 + 0.5 + 1.0 = 1.5
    consumed = se.compute_route_cap_consumed(conn, route_id=route_id)
    assert consumed == pytest.approx(1.5)


def test_compute_route_cap_consumed_at_cap(conn, store):
    route_id = _route_id()
    for _ in range(3):
        se.log_synthesis_event(
            conn, node_id=_node_id(), route_id=route_id, unit="u", province="p",
            stage="4_pure_synthesis", triggered_by_skill="x",
        )
    consumed = se.compute_route_cap_consumed(conn, route_id=route_id)
    assert consumed == pytest.approx(3.0)
    assert se.is_route_at_cap(consumed)


# ---------------------------------------------------------------------------
# 15. write is a single parameterised INSERT (no string-concat SQL)
# ---------------------------------------------------------------------------


def test_log_synthesis_event_uses_parameterised_insert(conn, store):
    # monkey-listen to cursor execute
    captured = []
    real_cursor = conn.cursor

    def spy_cursor(*a, **kw):
        cur = real_cursor(*a, **kw)
        real_exec = cur.execute

        def spy_exec(sql, params=None):
            captured.append((sql, tuple(params or ())))
            return real_exec(sql, params)

        cur.execute = spy_exec
        return cur

    conn.cursor = spy_cursor
    se.log_synthesis_event(
        conn,
        node_id=_node_id(),
        route_id=_route_id(),
        unit="u", province="p",
        stage="3a_poi_on_path",
        anchor_name="evil'; DROP TABLE node_prod.synthesis_events; --",
        triggered_by_skill="x",
    )
    assert captured, "execute was not called"
    sql, params = captured[0]
    # anchor_name is passed as a bind parameter, never inlined
    assert "evil" not in sql
    assert "DROP TABLE" not in sql
    assert "evil'; DROP TABLE node_prod.synthesis_events; --" in params


# ---------------------------------------------------------------------------
# 16. log_synthesis_event normalizes research_output_file basename
# ---------------------------------------------------------------------------


def test_log_synthesis_event_stores_basename_not_full_path(conn, store):
    se.log_synthesis_event(
        conn,
        node_id=_node_id(),
        route_id=_route_id(),
        unit="u", province="p",
        stage="3a_poi_on_path",
        research_output_file="workspace/research_queue/ingested/01_x.json",
        triggered_by_skill="x",
    )
    assert store.rows[0]["research_output_file"] == "01_x.json"
