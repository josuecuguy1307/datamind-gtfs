-- V4_8: the two tables the Phase 4 Evidence / Matching review needs.
--
-- semantics.route_evidence_records already exists. V4 and V4_2 defined the
-- tables below but were never applied to this database, and applying them
-- whole would also create seven unrelated tables (sample points, Overpass
-- candidates, ranker models...). This file creates only these two, keeping
-- V4/V4_2's column names, keys and relevance scale, plus who did what.
--
-- Nothing here writes rows. Apply explicitly:
--   python -m datamind_console.db.schema_cli plan phase4.evidence_matching
--   python -m datamind_console.db.schema_cli apply phase4.evidence_matching --yes

-- Candidate routes for one evidence record, produced by a named matcher when a
-- reviewer asks for them. A proposal only: nothing is accepted from here.
CREATE TABLE IF NOT EXISTS semantics.route_evidence_matches (
  match_id      uuid             PRIMARY KEY DEFAULT gen_random_uuid(),
  record_id     uuid             NOT NULL
                REFERENCES semantics.route_evidence_records(record_id) ON DELETE CASCADE,
  route_id      uuid             NOT NULL
                REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE,
  matcher       text             NOT NULL,
  score         double precision NOT NULL CHECK (score >= 0 AND score <= 1),
  score_parts   jsonb            NOT NULL DEFAULT '{}'::jsonb,
  is_best       boolean          NOT NULL DEFAULT false,
  requested_by  text             NOT NULL CHECK (length(btrim(requested_by)) >= 2),
  created_at    timestamptz      NOT NULL DEFAULT now(),
  CONSTRAINT uq_rem_record_route_matcher UNIQUE (record_id, route_id, matcher)
);

CREATE INDEX IF NOT EXISTS idx_rem_record_score
  ON semantics.route_evidence_matches (record_id, score DESC);

CREATE INDEX IF NOT EXISTS idx_rem_route_score
  ON semantics.route_evidence_matches (route_id, score DESC);

-- A reviewer's judgement of one (record, route) pair. Relevance follows
-- V4_2: 2 = correct match, 1 = partial, 0 = not this route. One label per
-- pair; a later decision replaces it and the audit trail in
-- console.phase_decisions keeps every change.
CREATE TABLE IF NOT EXISTS semantics.match_labels (
  label_id      bigserial    PRIMARY KEY,
  record_id     uuid         NOT NULL
                REFERENCES semantics.route_evidence_records(record_id) ON DELETE CASCADE,
  route_id      uuid         NOT NULL
                REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE,
  match_id      uuid         NULL
                REFERENCES semantics.route_evidence_matches(match_id) ON DELETE SET NULL,
  relevance     int          NOT NULL CHECK (relevance IN (0, 1, 2)),
  label_source  text         NOT NULL DEFAULT 'admin_ui',
  notes         text         NULL,
  reviewed_by   text         NOT NULL CHECK (length(btrim(reviewed_by)) >= 2),
  created_at    timestamptz  NOT NULL DEFAULT now(),
  updated_at    timestamptz  NOT NULL DEFAULT now(),
  CONSTRAINT uq_match_labels_record_route UNIQUE (record_id, route_id)
);

CREATE INDEX IF NOT EXISTS idx_match_labels_record
  ON semantics.match_labels (record_id, relevance DESC);

CREATE INDEX IF NOT EXISTS idx_match_labels_route
  ON semantics.match_labels (route_id, created_at DESC);
