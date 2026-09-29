-- 043 — extend re_entry_queue.classification to allow deep_research_legacy.
--
-- Why
-- ---
-- 55 routes across 10 sources (cayambe_cycle1, quito_norte cycle3*, duran
-- seeds_promote, valhalla_constructed, etc.) are stuck at status='failed'
-- with classification='unclassified' because their source_type is
-- 'deep_research_override' and the classifier had no map entry for it.
-- The fix in re_entry_classifier.py adds DEEP_RESEARCH_LEGACY; this
-- migration extends the DB CHECK constraint to accept that value.

ALTER TABLE route_prod.re_entry_queue
  DROP CONSTRAINT IF EXISTS re_entry_queue_classification_check;

ALTER TABLE route_prod.re_entry_queue
  ADD CONSTRAINT re_entry_queue_classification_check
  CHECK (classification IN (
    'osm_relation_severe',
    'osm_relation_clean',
    'discovery_legacy',
    'constructor_canonical_legacy',
    'manual_constructor_legacy',
    'structural_repair',
    'deep_research_legacy',
    'unclassified'
  ));
