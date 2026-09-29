from __future__ import annotations

from contextlib import contextmanager
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

_sentence_transformers = types.ModuleType("sentence_transformers")


class _DummySentenceTransformer:
    def __init__(self, *args, **kwargs):
        del args, kwargs

    def eval(self) -> None:
        return None


_sentence_transformers.SentenceTransformer = _DummySentenceTransformer
sys.modules.setdefault("sentence_transformers", _sentence_transformers)
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "phase2_semantics"))

from datamind_console.phases.phase2_semantics.client import Phase2Client
import datamind_console.phases.phase2_semantics.client as phase2_client_mod


class _FakeCursor:
    def __init__(self, *, existing_tables, table_columns=None, counts=None, max_ts=None, node_rows=None):
        self._existing_tables = set(existing_tables or set())
        self._table_columns = dict(table_columns or {})
        self._counts = dict(counts or {})
        self._max_ts = dict(max_ts or {})
        self._node_rows = list(node_rows or [])
        self._rows = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        normalized = " ".join(str(sql or "").split())
        params = params or ()
        self.rowcount = 0

        if normalized.startswith("SELECT to_regclass"):
            table = str(params[0])
            self._rows = [{"ok": table in self._existing_tables}]
            return

        if "FROM information_schema.columns" in normalized:
            schema = str(params[0])
            name = str(params[1])
            cols = self._table_columns.get(f"{schema}.{name}", [])
            self._rows = [{"column_name": c} for c in cols]
            return

        if normalized.startswith("SELECT COUNT(*)::int AS n FROM ") or normalized.startswith("SELECT COUNT(*) AS n FROM "):
            table = normalized.split("FROM ", 1)[1].split(" ", 1)[0].strip()
            self._rows = [{"n": int(self._counts.get(table, 0))}]
            return

        if normalized.startswith("SELECT MAX(updated_at) AS ts FROM "):
            table = normalized.split("FROM ", 1)[1].strip()
            self._rows = [{"ts": self._max_ts.get(table)}]
            return

        if "SELECT node_id::text AS node_id" in normalized and "FROM node_prod.nodes" in normalized:
            self._rows = list(self._node_rows)
            return

        raise AssertionError(f"Unhandled SQL in test fake cursor: {normalized}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self, **kwargs):
        self._kwargs = kwargs

    def cursor(self):
        return _FakeCursor(**self._kwargs)


class Phase2ClientStep20HandoffTests(unittest.TestCase):
    def test_run_step_20_build_candidates_prefers_script_stdout_place_set_id(self) -> None:
        class _Client(Phase2Client):
            def _run_script(self, script_name, *, args=None, env_overrides=None):
                del script_name, args, env_overrides
                return {
                    "ok": True,
                    "returncode": 0,
                    "stdout": "\n".join(
                        [
                            json.dumps({"message": "Starting candidate construction"}),
                            json.dumps({"message": "done", "place_set_id": "ps-stdout"}),
                        ]
                    ),
                    "stderr": "",
                    "script": "20_build_candidates.py",
                }

            def get_latest_place_set_id(self, *, context_key=None):
                del context_key
                return "ps-db-fallback"

        client = _Client()
        out = client.run_step_20_build_candidates(context_key="ctx-1")
        summary = dict(out.get("summary") or {})

        self.assertEqual(summary.get("place_set_id"), "ps-stdout")
        self.assertEqual(summary.get("place_set_id_source"), "script_stdout")

    def test_run_step_20_build_candidates_falls_back_to_context_lookup(self) -> None:
        class _Client(Phase2Client):
            def _run_script(self, script_name, *, args=None, env_overrides=None):
                del script_name, args, env_overrides
                return {
                    "ok": True,
                    "returncode": 0,
                    "stdout": json.dumps({"message": "done"}),
                    "stderr": "",
                    "script": "20_build_candidates.py",
                }

            def get_latest_place_set_id(self, *, context_key=None):
                self.context_key = context_key
                return "ps-context"

        client = _Client()
        out = client.run_step_20_build_candidates(context_key="ctx-2")
        summary = dict(out.get("summary") or {})

        self.assertEqual(summary.get("place_set_id"), "ps-context")
        self.assertEqual(summary.get("place_set_id_source"), "context_lookup")
        self.assertEqual(getattr(client, "context_key", None), "ctx-2")


class Phase2ClientRefreshPlanTests(unittest.TestCase):
    def test_get_step40_refresh_plan_detects_missing_place_embeddings(self) -> None:
        fake_conn = _FakeConn(
            existing_tables={
                "geo_prod.place_aliases",
                "geo_prod.place_alias_embeddings",
                "geo_prod.places",
                "geo_prod.place_embeddings",
            },
            counts={
                "geo_prod.place_aliases": 10,
                "geo_prod.place_alias_embeddings": 10,
                "geo_prod.places": 5,
                "geo_prod.place_embeddings": 0,
            },
            max_ts={
                "geo_prod.place_aliases": "2026-03-10T00:00:00+00:00",
                "geo_prod.place_alias_embeddings": "2026-03-10T00:00:00+00:00",
                "geo_prod.places": "2026-03-10T00:00:00+00:00",
                "geo_prod.place_embeddings": None,
            },
        )

        @contextmanager
        def fake_conn_ctx():
            yield fake_conn

        with mock.patch.object(phase2_client_mod, "_conn_ctx", fake_conn_ctx):
            plan = Phase2Client().get_step40_refresh_plan()

        self.assertTrue(plan["should_run"])
        self.assertEqual(plan["reason"], "places_embedding_count_mismatch")
        self.assertEqual(plan["alias_reason"], "embeddings_up_to_date")
        self.assertEqual(plan["place_reason"], "embedding_count_mismatch")
        self.assertEqual(plan["place_embedding_count"], 0)


class Phase2ClientNormalizerTests(unittest.TestCase):
    def test_normalize_prod_nodes_global_applies_limit_and_guardrail_in_dry_run(self) -> None:
        fake_conn = _FakeConn(
            existing_tables={"node_prod.nodes"},
            table_columns={
                "node_prod.nodes": {"node_id", "node_type", "name", "ref", "updated_at", "lat", "lon"},
            },
            node_rows=[
                {
                    "node_id": "a",
                    "lat": 0.0,
                    "lon": 0.0,
                    "node_type": "STOP",
                    "name": "Stop A",
                    "ref": "A1",
                    "updated_at": "2026-03-10T00:00:00+00:00",
                },
                {
                    "node_id": "b",
                    "lat": 0.0,
                    "lon": 0.000001,
                    "node_type": "STOP",
                    "name": "node_123",
                    "ref": "",
                    "updated_at": "2026-03-10T00:00:00+00:00",
                },
                {
                    "node_id": "c",
                    "lat": 1.0,
                    "lon": 1.0,
                    "node_type": "STOP",
                    "name": "Stop C",
                    "ref": "C1",
                    "updated_at": "2026-03-10T00:00:00+00:00",
                },
                {
                    "node_id": "d",
                    "lat": 1.0,
                    "lon": 1.000001,
                    "node_type": "STOP",
                    "name": "node_999",
                    "ref": "",
                    "updated_at": "2026-03-10T00:00:00+00:00",
                },
            ],
        )

        @contextmanager
        def fake_conn_ctx():
            yield fake_conn

        with mock.patch.object(phase2_client_mod, "_conn_ctx", fake_conn_ctx):
            preview = Phase2Client().normalize_prod_nodes_global(
                dedup_radius_m=2.0,
                apply_delete_limit=2,
                max_delete_apply=1,
                dry_run=True,
            )

        self.assertEqual(preview["final_delete_candidates"], 2)
        self.assertEqual(preview["selected_delete_candidates"], 2)
        self.assertTrue(preview["guardrail_blocked"])
        self.assertEqual(preview["sample_deletes"][0]["reason"], "duplicate")
        self.assertEqual(preview["selected_delete_ids_sample"], ["b", "d"])

    def test_normalize_prod_nodes_global_keeps_distinct_good_names_in_same_spatial_cluster(self) -> None:
        fake_conn = _FakeConn(
            existing_tables={"node_prod.nodes"},
            table_columns={
                "node_prod.nodes": {"node_id", "node_type", "name", "ref", "updated_at", "lat", "lon"},
            },
            node_rows=[
                {
                    "node_id": "a",
                    "lat": 0.0,
                    "lon": 0.0,
                    "node_type": "STOP",
                    "name": "Terminal Norte",
                    "ref": "TN",
                    "updated_at": "2026-03-10T00:00:00+00:00",
                },
                {
                    "node_id": "b",
                    "lat": 0.0,
                    "lon": 0.000001,
                    "node_type": "STOP",
                    "name": "Parque Central",
                    "ref": "PC",
                    "updated_at": "2026-03-10T00:00:00+00:00",
                },
            ],
        )

        @contextmanager
        def fake_conn_ctx():
            yield fake_conn

        with mock.patch.object(phase2_client_mod, "_conn_ctx", fake_conn_ctx):
            preview = Phase2Client().normalize_prod_nodes_global(
                dedup_radius_m=2.0,
                delete_bad_named_nodes=False,
                dry_run=True,
            )

        self.assertEqual(preview["duplicate_clusters"], 1)
        self.assertEqual(preview["final_delete_candidates"], 0)
        self.assertEqual(preview["rename_candidates"], 0)

    def test_normalize_prod_nodes_global_merges_similar_names_and_generic_placeholder(self) -> None:
        fake_conn = _FakeConn(
            existing_tables={"node_prod.nodes"},
            table_columns={
                "node_prod.nodes": {"node_id", "node_type", "name", "ref", "updated_at", "lat", "lon"},
            },
            node_rows=[
                {
                    "node_id": "a",
                    "lat": 0.0,
                    "lon": 0.0,
                    "node_type": "STOP",
                    "name": "Av. Amazonas y Colon",
                    "ref": "A1",
                    "updated_at": "2026-03-10T00:00:00+00:00",
                },
                {
                    "node_id": "b",
                    "lat": 0.0,
                    "lon": 0.000001,
                    "node_type": "STOP",
                    "name": "Av Amazonas y Colón",
                    "ref": "A1",
                    "updated_at": "2026-03-10T00:00:00+00:00",
                },
                {
                    "node_id": "c",
                    "lat": 0.0,
                    "lon": 0.0000015,
                    "node_type": "STOP",
                    "name": "node_123",
                    "ref": "",
                    "updated_at": "2026-03-10T00:00:00+00:00",
                },
            ],
        )

        @contextmanager
        def fake_conn_ctx():
            yield fake_conn

        with mock.patch.object(phase2_client_mod, "_conn_ctx", fake_conn_ctx):
            preview = Phase2Client().normalize_prod_nodes_global(
                dedup_radius_m=2.0,
                delete_bad_named_nodes=False,
                dry_run=True,
            )

        self.assertEqual(preview["duplicate_clusters"], 1)
        self.assertEqual(preview["final_delete_candidates"], 2)
        self.assertEqual(preview["selected_delete_ids_sample"], ["b", "c"])

    def test_normalize_prod_nodes_global_keeps_conflicting_refs_apart(self) -> None:
        fake_conn = _FakeConn(
            existing_tables={"node_prod.nodes"},
            table_columns={
                "node_prod.nodes": {"node_id", "node_type", "name", "ref", "updated_at", "lat", "lon"},
            },
            node_rows=[
                {
                    "node_id": "a",
                    "lat": 0.0,
                    "lon": 0.0,
                    "node_type": "STOP",
                    "name": "Av. Amazonas y Colon",
                    "ref": "STOP001",
                    "updated_at": "2026-03-10T00:00:00+00:00",
                },
                {
                    "node_id": "b",
                    "lat": 0.0,
                    "lon": 0.000001,
                    "node_type": "STOP",
                    "name": "Av Amazonas y Colón",
                    "ref": "STOP002",
                    "updated_at": "2026-03-10T00:00:00+00:00",
                },
            ],
        )

        @contextmanager
        def fake_conn_ctx():
            yield fake_conn

        with mock.patch.object(phase2_client_mod, "_conn_ctx", fake_conn_ctx):
            preview = Phase2Client().normalize_prod_nodes_global(
                dedup_radius_m=2.0,
                delete_bad_named_nodes=False,
                dry_run=True,
            )

        self.assertEqual(preview["duplicate_clusters"], 1)
        self.assertEqual(preview["final_delete_candidates"], 0)


if __name__ == "__main__":
    unittest.main()
