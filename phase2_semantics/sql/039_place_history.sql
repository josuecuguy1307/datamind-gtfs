-- Block 7: Place versioning — capture previous state before updates.

CREATE TABLE IF NOT EXISTS geo_prod.place_history (
    history_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    place_id     uuid NOT NULL,
    previous_canonical_name text,
    previous_place_type     text,
    previous_status         text,
    previous_geom           geometry(Point, 4326),
    changed_by   text NOT NULL,
    changed_at   timestamptz NOT NULL DEFAULT now(),
    change_reason text
);

CREATE INDEX IF NOT EXISTS idx_place_history_place_id
    ON geo_prod.place_history (place_id);
CREATE INDEX IF NOT EXISTS idx_place_history_changed_at
    ON geo_prod.place_history (changed_at);

-- Trigger: automatically capture history on meaningful changes.
CREATE OR REPLACE FUNCTION geo_prod.capture_place_history()
RETURNS trigger AS $$
BEGIN
    IF OLD.canonical_name IS DISTINCT FROM NEW.canonical_name
       OR OLD.place_type IS DISTINCT FROM NEW.place_type
       OR OLD.status IS DISTINCT FROM NEW.status THEN
        INSERT INTO geo_prod.place_history
            (place_id, previous_canonical_name, previous_place_type,
             previous_status, previous_geom, changed_by, change_reason)
        VALUES
            (OLD.place_id, OLD.canonical_name, OLD.place_type,
             OLD.status, OLD.geom,
             COALESCE(current_setting('app.changed_by', true), 'unknown'),
             current_setting('app.change_reason', true));
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_place_history ON geo_prod.places;
CREATE TRIGGER trg_place_history
BEFORE UPDATE ON geo_prod.places
FOR EACH ROW EXECUTE FUNCTION geo_prod.capture_place_history();
