"""Phase 4 Evidence / Matching: deterministic proposals, validated human writes, atomic audit."""
from __future__ import annotations

import ast
from contextlib import contextmanager
from pathlib import Path

import pytest

from datamind_console.phases.phase4_naming import evidence_matching as em
from datamind_console.review.human_review import Reviewer, ReviewInputError

REC = "11111111-1111-4111-8111-111111111111"
ROUTE = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# A fake connection that records statements and can fail the audit insert.
# ---------------------------------------------------------------------------

class _Cursor:
    def __init__(self, db):
        self.db = db
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        up = " ".join(sql.split()).upper()
        self.db.statements.append(up)
        if up.startswith("INSERT INTO SEMANTICS.ROUTE_EVIDENCE_RECORDS"):
            self.db.records.append(params)
            self._row = {"record_id": REC}
        elif up.startswith("INSERT INTO SEMANTICS.ROUTE_EVIDENCE_MATCHES"):
            self.db.matches[(params[0], params[1], params[2])] = params
            self._row = {"match_id": f"m-{params[1][:4]}"}
        elif up.startswith("WITH GONE AS"):
            self._row = {"n": 0}
        elif up.startswith("INSERT INTO SEMANTICS.MATCH_LABELS"):
            self.db.labels[(params[0], params[1])] = params
            self._row = {"label_id": len(self.db.labels)}
        elif up.startswith("INSERT INTO CONSOLE.PHASE_DECISIONS"):
            if self.db.fail_audit:
                raise RuntimeError("audit insert failed")
            self.db.decisions.append(params)
            self._row = {"decision_id": f"d{len(self.db.decisions)}", "created_at": "now"}
        else:
            raise AssertionError(f"unexpected SQL: {up[:80]}")

    def fetchone(self):
        return self._row


class _FakeDb:
    def __init__(self):
        self.records, self.decisions, self.statements = [], [], []
        self.matches, self.labels = {}, {}
        self.fail_audit = False
        self.committed = self.rolled_back = 0

    def cursor(self, *a, **k):
        return _Cursor(self)

    @contextmanager
    def transaction(self):
        snapshot = (list(self.records), dict(self.matches), dict(self.labels), list(self.decisions))
        try:
            yield self
            self.committed += 1
        except Exception:
            self.records, self.matches, self.labels, self.decisions = (list(snapshot[0]), dict(snapshot[1]),
                                                                       dict(snapshot[2]), list(snapshot[3]))
            self.rolled_back += 1
            raise


@pytest.fixture()
def db():
    return _FakeDb()


WHO = Reviewer("Ana Revisora")


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def test_missing_signals_are_left_out_and_weights_renormalised() -> None:
    score, parts = em.score_route({"route_id": ROUTE, "name": 0.8})
    assert score == pytest.approx(0.8)
    assert parts["weights"] == {"name": 1.0} and parts["matcher"] == em.MATCHER


def test_endpoints_accept_either_direction() -> None:
    forward = {"from_first": 1.0, "to_last": 1.0, "from_last": 0.1, "to_first": 0.1}
    backward = {"from_first": 0.1, "to_last": 0.1, "from_last": 1.0, "to_first": 1.0}
    assert em._endpoints(forward) == em._endpoints(backward) == 1.0
    assert em._endpoints({}) is None


def test_ranking_is_deterministic_explained_and_marks_one_best() -> None:
    rows = [
        {"route_id": OTHER, "route_name": "B", "ref": 0.0, "name": 0.9},
        {"route_id": ROUTE, "route_name": "A", "ref": 1.0, "name": 0.9},
        {"route_id": "44444444-4444-4444-8444-444444444444", "route_name": "C", "ref": 0.0, "name": 0.1},
    ]
    ranked = em.rank_candidates(rows, limit=10)
    assert [c["route_id"] for c in ranked] == [ROUTE, OTHER]  # C is below the floor
    assert [c["is_best"] for c in ranked] == [True, False]
    assert em.rank_candidates(list(reversed(rows)), limit=10) == ranked
    assert set(ranked[0]["score_parts"]["signals"]) == {"ref", "name"}


def test_an_exact_ref_or_hint_is_always_proposed() -> None:
    ranked = em.rank_candidates([{"route_id": ROUTE, "ref": 1.0, "name": 0.0, "operator": 0.0}], min_score=0.99)
    assert ranked and ranked[0]["route_id"] == ROUTE


def test_a_record_with_nothing_to_compare_gets_no_candidates() -> None:
    assert em.rank_candidates([{"route_id": ROUTE}, {"route_id": OTHER}]) == []


# ---------------------------------------------------------------------------
# Evidence creation: validated human action, atomic with its audit row
# ---------------------------------------------------------------------------

def _create(db, **over):
    kw = dict(reviewer=WHO, source_type="official_schedule", fields={"route_ref": "E1", "route_name": "Ecovía"},
              title="Horario oficial", note="PDF del operador", confidence_hint=0.8, transaction_factory=db.transaction)
    kw.update(over)
    return em.create_evidence(**kw)


def test_create_evidence_writes_record_and_audit_together(db) -> None:
    out = _create(db)
    assert out["record_id"] == REC and len(db.records) == 1 and len(db.decisions) == 1
    phase, item_id, decision, reason_code, notes, _user = db.decisions[0]
    assert (phase, item_id, decision, reason_code) == (4, f"evidence:{REC}", "EDIT", "evidence_created")
    assert "Ana Revisora" in notes
    raw = db.records[0][-1]
    assert '"created_by": "Ana Revisora"' in raw and '"origin": "console_manual"' in raw


def test_create_evidence_rolls_back_when_audit_fails(db) -> None:
    db.fail_audit = True
    with pytest.raises(RuntimeError):
        _create(db)
    assert db.records == [] and db.decisions == [] and db.rolled_back == 1


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"reviewer": Reviewer(" ")}, "reviewer_required"),
        ({"source_type": "ai_proposal"}, "unknown_source_type"),
        ({"fields": {"route_ref": "  "}}, "evidence_empty"),
        ({"confidence_hint": 1.5}, "confidence_invalid"),
        ({"note": "x" * (em.MAX_NOTE_CHARS + 1)}, "note_too_long"),
    ],
)
def test_create_evidence_refuses_bad_input_before_writing(db, over, code) -> None:
    with pytest.raises(ReviewInputError, match=code):
        _create(db, **over)
    assert db.statements == []


# ---------------------------------------------------------------------------
# Matching: proposals and labels
# ---------------------------------------------------------------------------

def _signals(rows):
    def query(sql, params=None):
        return rows if "WITH ev AS" in sql else [{"?column?": 1}]
    return query


def test_generate_candidates_stores_proposals_only(db) -> None:
    rows = [{"route_id": ROUTE, "route_name": "A", "ref": 1.0, "name": 0.9}, {"route_id": OTHER, "route_name": "B", "name": 0.1}]
    out = em.generate_candidates(REC, reviewer=WHO, query=_signals(rows), transaction_factory=db.transaction)
    assert [c["route_id"] for c in out["items"]] == [ROUTE] and out["routes_scored"] == 2
    assert list(db.matches) == [(REC, ROUTE, em.MATCHER)]
    assert db.matches[(REC, ROUTE, em.MATCHER)][-1] == "Ana Revisora"  # requested_by
    assert db.labels == {} and db.decisions == []  # a proposal is never a decision


def test_generate_candidates_is_idempotent(db) -> None:
    rows = [{"route_id": ROUTE, "route_name": "A", "ref": 1.0}]
    first = em.generate_candidates(REC, reviewer=WHO, query=_signals(rows), transaction_factory=db.transaction)
    second = em.generate_candidates(REC, reviewer=WHO, query=_signals(rows), transaction_factory=db.transaction)
    assert first["items"] == second["items"] and len(db.matches) == 1
    assert all("ON CONFLICT (RECORD_ID, ROUTE_ID, MATCHER) DO UPDATE" in s for s in db.statements if "ROUTE_EVIDENCE_MATCHES (" in s)


def test_generate_candidates_needs_a_reviewer_and_an_existing_record(db) -> None:
    with pytest.raises(ReviewInputError, match="reviewer_required"):
        em.generate_candidates(REC, reviewer=Reviewer(""), query=_signals([]), transaction_factory=db.transaction)
    with pytest.raises(ReviewInputError, match="evidence_not_found"):
        em.generate_candidates(REC, reviewer=WHO, query=lambda sql, p=None: [], transaction_factory=db.transaction)
    with pytest.raises(ReviewInputError, match="invalid_id"):
        em.generate_candidates("not-a-uuid", reviewer=WHO, query=_signals([]), transaction_factory=db.transaction)


@pytest.mark.parametrize(("relevance", "decision", "reason"), [(2, "APPROVE", "evidence_match_confirmed"),
                                                               (1, "EDIT", "evidence_match_partial"),
                                                               (0, "REJECT", "evidence_match_rejected")])
def test_label_writes_label_and_audit_together(db, relevance, decision, reason) -> None:
    em.label_match(REC, ROUTE, relevance, reviewer=WHO, notes="checked map", transaction_factory=db.transaction)
    assert db.labels[(REC, ROUTE)][3] == relevance and db.labels[(REC, ROUTE)][-1] == "Ana Revisora"
    assert db.decisions[0][:4] == (4, f"evidence_match:{REC}:{ROUTE}", decision, reason)


def test_relabelling_updates_the_label_and_keeps_every_audit_row(db) -> None:
    em.label_match(REC, ROUTE, 2, reviewer=WHO, transaction_factory=db.transaction)
    em.label_match(REC, ROUTE, 0, reviewer=WHO, notes="wrong corridor", transaction_factory=db.transaction)
    assert len(db.labels) == 1 and db.labels[(REC, ROUTE)][3] == 0
    assert [d[2] for d in db.decisions] == ["APPROVE", "REJECT"]
    assert all("ON CONFLICT (RECORD_ID, ROUTE_ID) DO UPDATE" in s for s in db.statements if "MATCH_LABELS (" in s)


def test_label_rolls_back_when_audit_fails(db) -> None:
    db.fail_audit = True
    with pytest.raises(RuntimeError):
        em.label_match(REC, ROUTE, 2, reviewer=WHO, transaction_factory=db.transaction)
    assert db.labels == {} and db.decisions == []


@pytest.mark.parametrize(("args", "code"), [((REC, ROUTE, 3), "unknown_relevance"), ((REC, ROUTE, 0), "reason_required"),
                                            (("x", ROUTE, 2), "invalid_id")])
def test_label_refuses_bad_input_before_writing(db, args, code) -> None:
    with pytest.raises(ReviewInputError, match=code):
        em.label_match(*args, reviewer=WHO, transaction_factory=db.transaction)
    assert db.statements == []


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------

def test_list_evidence_rejects_unknown_status() -> None:
    with pytest.raises(ReviewInputError):
        em.list_evidence(status="approved", query=lambda *a: [])


def test_tabs_open_without_ddl_and_never_write_without_a_button() -> None:
    """Rendering only reads; every write call sits behind a button or form submit."""
    writes = {"create_evidence", "generate_candidates", "label_match"}
    for name in ("evidence_tab.py", "matching_tab.py"):
        path = ROOT / "datamind_console/views/phases/phase4/tabs" / name
        text = path.read_text(encoding="utf-8")
        assert "schema_cli" not in text and "apply(" not in text and "CREATE TABLE" not in text.upper()
        tree = ast.parse(text)
        for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
            calls = {c.func.attr for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
            if calls & writes:
                src = ast.get_source_segment(text, fn) or ""
                assert "st.button(" in src or "form_submit_button(" in src, (name, fn.name)


def test_service_has_no_auto_approval_path() -> None:
    text = (ROOT / "datamind_console/phases/phase4_naming/evidence_matching.py").read_text(encoding="utf-8")
    assert text.count("INSERT INTO semantics.match_labels") == 1  # only label_match writes labels
    funcs = {n.name: n for n in ast.walk(ast.parse(text)) if isinstance(n, ast.FunctionDef)}
    for name in ("create_evidence", "generate_candidates", "label_match"):
        args = funcs[name].args
        required = [a.arg for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is None]
        assert "reviewer" in required, name  # every write names a person; there is no default


def test_reviewer_field_is_drawn_once_per_page() -> None:
    """Evidence and Matching share one page; a second reviewer_identity() would be a duplicate widget key."""
    tabs = ROOT / "datamind_console/views/phases/phase4/tabs"
    calls = sum((tabs / n).read_text(encoding="utf-8").count("reviewer_identity()") for n in ("evidence_tab.py", "matching_tab.py"))
    assert calls == 1
