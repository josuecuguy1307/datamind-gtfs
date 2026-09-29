-- 033_re_entry_fk_deferrable.sql
-- Make re_entry_queue → routes FK deferrable so the swap service can
-- bump `routes.version` and `re_entry_queue.current_version` atomically
-- inside the same transaction without the FK firing mid-statement.
--
-- The swap updates routes.version 1→2 in place (Option B architecture);
-- the queue row that still references (route_id, 1) is updated to
-- (route_id, 2, status='swapped') in the same transaction. Without
-- DEFERRABLE, either order trips the FK.

ALTER TABLE route_prod.re_entry_queue
  DROP CONSTRAINT IF EXISTS fk_re_entry_queue_route;

ALTER TABLE route_prod.re_entry_queue
  ADD CONSTRAINT fk_re_entry_queue_route
    FOREIGN KEY (route_id, current_version)
    REFERENCES route_prod.routes (route_id, version)
    ON DELETE CASCADE
    DEFERRABLE INITIALLY IMMEDIATE;
