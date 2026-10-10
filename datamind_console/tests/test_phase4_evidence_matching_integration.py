"""Phase 4 Evidence / Matching against real Postgres: V4_8 constraints, real matcher SQL, rollback.

Opt-in and disposable-only, like test_human_review_integration: runs only when
DATAMIND_DISPOSABLE_TEST_DSN names a database whose name contains "test",
"scratch" or "disposable", and is not datamind_ml. That database needs the
schema of semantics.route_evidence_records, route_raw.route_jobs,
route_prod.routes, route_prod.route_semantics, node_prod.nodes and
console.phase_decisions (``pg_dump -s -t ...``), the pg_trgm / unaccent /
postgis extensions, and V4_8 applied with ``schema_cli apply
phase4.evidence_matching``.
"""
from __future__ import annotations

import os
import uuid
from contextlib import contextmanager

import pytest

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402
from psycopg2.extensions import parse_dsn  # noqa: E402

from datamind_console.phases.phase4_naming import evidence_matching as em  # noqa: E402
from datamind_console.review.human_review import Reviewer, ReviewInputError  # noqa: E402

DSN = os.environ.get("DATAMIND_DISPOSABLE_TEST_DSN", "").strip()
WHO = Reviewer("Integration Reviewer")


def _disposable(dsn: str) -> bool:
    try:
        name = (parse_dsn(dsn).get("dbname") or "").lower()
    except Exception:
        return False
    return bool(name) and name != "datamind_ml" and any(m in name for m in ("test", "scratch", "disposable"))


pytestmark = pytest.mark.skipif(not _disposable(DSN), reason="set DATAMIND_DISPOSABLE_TEST_DSN to a disposable database")


@contextmanager
def _tx():
    conn = psycopg2.connect(DSN)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _query(sql, params=None):
    with _tx() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or ())
        return [dict(r) for r in cur.fetchall()]


def _one(sql, params=None):
    rows = _query(sql, params)
    return rows[0] if rows else None


ROUTES = [
    # (ref, name, first stop, last stop, x0, x1)
    ("E1", "Ecovía Río Coca - Quitumbe", "Río Coca", "Quitumbe", -78.48, -78.52),
    ("T3", "Troncal 3 Bastión - IESS", "Bastión", "IESS", -79.90, -79.88),
]


@pytest.fixture()
def seeded():
    ids = {"routes": [], "nodes": [], "records": []}
    with _tx() as conn, conn.cursor() as cur:
        for ref, name, first, last, x0, x1 in ROUTES:
            rid, n1, n2 = (str(uuid.uuid4()) for _ in range(3))
            for nid, nname, x in ((n1, first, x0), (n2, last, x1)):
                cur.execute("INSERT INTO node_prod.nodes (node_id, geom, node_type, name) "
                            "VALUES (%s, ST_SetSRID(ST_MakePoint(%s, -0.2), 4326), 'STOP', %s)", (nid, x, nname))
            cur.execute("INSERT INTO route_raw.route_jobs (route_id, known_ref) VALUES (%s, %s)", (rid, ref))
            cur.execute(
                "INSERT INTO route_prod.routes (route_id, geom, stop_node_ids, route_name, legacy_grandfathered, "
                " grandfathered_until, source_type) VALUES (%s, ST_SetSRID(ST_MakeLine(ST_MakePoint(%s, -0.2), "
                " ST_MakePoint(%s, -0.2)), 4326), ARRAY[%s, %s]::uuid[], %s, true, now() + interval '1 year', "
                " 'constructor_canonical')",
                (rid, x0, x1, n1, n2, name),
            )
            cur.execute("INSERT INTO route_prod.route_semantics (route_id, route_name, route_ref, operator_name) "
                        "VALUES (%s, %s, %s, 'Operadora Test')", (rid, name, ref))
            ids["routes"].append(rid)
            ids["nodes"] += [n1, n2]
    yield ids
    with _tx() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM console.phase_decisions WHERE item_id LIKE 'evidence%%' AND notes LIKE %s",
                    (f"%{WHO.name}%",))
        cur.execute("DELETE FROM semantics.route_evidence_records WHERE raw->>'created_by' = %s", (WHO.name,))
        cur.execute("DELETE FROM route_prod.route_semantics WHERE route_id = ANY(%s::uuid[])", (ids["routes"],))
        cur.execute("DELETE FROM route_prod.routes WHERE route_id = ANY(%s::uuid[])", (ids["routes"],))
        cur.execute("DELETE FROM route_raw.route_jobs WHERE route_id = ANY(%s::uuid[])", (ids["routes"],))
        cur.execute("DELETE FROM node_prod.nodes WHERE node_id = ANY(%s::uuid[])", (ids["nodes"],))


def _create(**fields):
    return em.create_evidence(reviewer=WHO, source_type="official_schedule", fields=fields, title="Test schedule",
                              transaction_factory=_tx)["record_id"]


def test_v4_8_objects_and_constraints_exist() -> None:
    cons = {r["conname"] for r in _query(
        "SELECT conname FROM pg_constraint WHERE conrelid IN "
        "('semantics.match_labels'::regclass, 'semantics.route_evidence_matches'::regclass)")}
    assert {"uq_match_labels_record_route", "uq_rem_record_route_matcher", "match_labels_relevance_check",
            "match_labels_reviewed_by_check", "match_labels_route_id_fkey", "route_evidence_matches_route_id_fkey"} <= cons


def test_create_list_detail_and_audit(seeded) -> None:
    token = f"Ecovía-{uuid.uuid4().hex[:8]}"  # unique, so other rows in the database cannot match
    rid = _create(route_ref="E1", route_name=token, from_name="Rio Coca", to_name="Quitumbe")
    audit = _one("SELECT phase, decision, reason_code FROM console.phase_decisions WHERE item_id = %s", (f"evidence:{rid}",))
    assert audit == {"phase": 4, "decision": "EDIT", "reason_code": "evidence_created"}
    page = em.list_evidence(search=token.lower(), query=_query)
    assert [r["record_id"] for r in page["items"]] == [rid] and page["items"][0]["status"] == "pending"
    detail = em.evidence_detail(rid, query=_query, query1=_one)
    assert detail["raw"]["created_by"] == WHO.name and detail["labels"] == []


def test_matcher_ranks_the_right_route_and_is_idempotent(seeded) -> None:
    e1 = seeded["routes"][0]
    rid = _create(route_ref="e1", from_name="Quitumbe", to_name="Rio Coca")  # reverse direction, no accents
    first = em.generate_candidates(rid, reviewer=WHO, query=_query, transaction_factory=_tx)
    top = first["items"][0]["score"]
    best = {c["route_id"] for c in first["items"] if c["score"] == top}  # other identical routes in the DB may tie
    assert e1 in best and top == 1.0 and first["items"][0]["is_best"]
    assert next(c for c in first["items"] if c["route_id"] == e1)["score_parts"]["signals"]["ref"] == 1.0
    assert seeded["routes"][1] not in best
    em.generate_candidates(rid, reviewer=WHO, query=_query, transaction_factory=_tx)
    stored = _query("SELECT route_id::text, requested_by FROM semantics.route_evidence_matches WHERE record_id = %s", (rid,))
    assert len(stored) == len(first["items"]) and all(r["requested_by"] == WHO.name for r in stored)
    assert _one("SELECT count(*) AS n FROM semantics.match_labels WHERE record_id = %s", (rid,))["n"] == 0


def test_label_upserts_and_audits_every_change(seeded) -> None:
    e1 = seeded["routes"][0]
    rid = _create(route_ref="E1")
    em.label_match(rid, e1, 2, reviewer=WHO, transaction_factory=_tx)
    em.label_match(rid, e1, 0, reviewer=WHO, notes="wrong corridor", transaction_factory=_tx)
    label = _one("SELECT relevance, reviewed_by FROM semantics.match_labels WHERE record_id = %s", (rid,))
    assert label == {"relevance": 0, "reviewed_by": WHO.name}
    audits = _query("SELECT decision FROM console.phase_decisions WHERE item_id = %s ORDER BY created_at",
                    (f"evidence_match:{rid}:{e1}",))
    assert [a["decision"] for a in audits] == ["APPROVE", "REJECT"]
    assert rid in [r["record_id"] for r in em.list_evidence(status="rejected", limit=500, query=_query)["items"]]


def test_label_for_unknown_route_rolls_back_with_no_audit(seeded) -> None:
    rid = _create(route_ref="E1")
    ghost = str(uuid.uuid4())
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        em.label_match(rid, ghost, 2, reviewer=WHO, transaction_factory=_tx)
    assert _one("SELECT count(*) AS n FROM console.phase_decisions WHERE item_id = %s", (f"evidence_match:{rid}:{ghost}",))["n"] == 0


def test_database_refuses_bad_rows_directly(seeded) -> None:
    rid = _create(route_ref="E1")
    e1 = seeded["routes"][0]
    for sql, params in (
        ("INSERT INTO semantics.match_labels (record_id, route_id, relevance, reviewed_by) VALUES (%s, %s, 5, 'Ana')", (rid, e1)),
        ("INSERT INTO semantics.match_labels (record_id, route_id, relevance, reviewed_by) VALUES (%s, %s, 2, ' ')", (rid, e1)),
        ("INSERT INTO semantics.route_evidence_matches (record_id, route_id, matcher, score, requested_by) "
         "VALUES (%s, %s, 'm', 1.5, 'Ana')", (rid, e1)),
    ):
        with pytest.raises(psycopg2.errors.CheckViolation):
            with _tx() as conn, conn.cursor() as cur:
                cur.execute(sql, params)


def test_deleting_evidence_cascades_to_its_matches_and_labels(seeded) -> None:
    e1 = seeded["routes"][0]
    rid = _create(route_ref="E1")
    em.generate_candidates(rid, reviewer=WHO, query=_query, transaction_factory=_tx)
    em.label_match(rid, e1, 2, reviewer=WHO, transaction_factory=_tx)
    with _tx() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM semantics.route_evidence_records WHERE record_id = %s", (rid,))
    for table in ("route_evidence_matches", "match_labels"):
        assert _one(f"SELECT count(*) AS n FROM semantics.{table} WHERE record_id = %s", (rid,))["n"] == 0


def test_bad_input_never_reaches_the_database(seeded) -> None:
    before = _one("SELECT count(*) AS n FROM semantics.route_evidence_records")["n"]
    with pytest.raises(ReviewInputError):
        em.create_evidence(reviewer=WHO, source_type="official_schedule", fields={}, transaction_factory=_tx)
    assert _one("SELECT count(*) AS n FROM semantics.route_evidence_records")["n"] == before
