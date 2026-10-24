-- A recommendation is for its flight's airframe, always (spec: Shared storage and sync,
-- "No duplicates"). When one log is imported on two laptops onto different airframes,
-- the two flights become one, on one airframe; each laptop's recommendations for either
-- copy must follow it there, or laptops that saw the copies in a different order keep
-- different airframes for the same recommendation. The same rule holds on every laptop,
-- so it is kept here rather than synced: nothing it changes goes out as a change.

CREATE TRIGGER recommendation_follows_its_flight
AFTER UPDATE OF airframe_id ON flight
WHEN NEW.airframe_id IS NOT NULL
BEGIN
    UPDATE recommendation SET airframe_id = NEW.airframe_id
    WHERE flight_id = NEW.id AND airframe_id IS NOT NEW.airframe_id;
END;

CREATE TRIGGER recommendation_made_for_its_flight
AFTER INSERT ON recommendation
WHEN NEW.flight_id IS NOT NULL
BEGIN
    UPDATE recommendation
    SET airframe_id = (SELECT airframe_id FROM flight WHERE id = NEW.flight_id)
    WHERE id = NEW.id
      AND (SELECT airframe_id FROM flight WHERE id = NEW.flight_id) IS NOT NULL
      AND airframe_id IS NOT (SELECT airframe_id FROM flight WHERE id = NEW.flight_id);
END;

CREATE TRIGGER recommendation_kept_with_its_flight
AFTER UPDATE OF flight_id, airframe_id ON recommendation
WHEN NEW.flight_id IS NOT NULL
BEGIN
    UPDATE recommendation
    SET airframe_id = (SELECT airframe_id FROM flight WHERE id = NEW.flight_id)
    WHERE id = NEW.id
      AND (SELECT airframe_id FROM flight WHERE id = NEW.flight_id) IS NOT NULL
      AND airframe_id IS NOT (SELECT airframe_id FROM flight WHERE id = NEW.flight_id);
END;

-- and the ones already kept
UPDATE recommendation
SET airframe_id = (SELECT f.airframe_id FROM flight f WHERE f.id = recommendation.flight_id)
WHERE flight_id IS NOT NULL
  AND (SELECT f.airframe_id FROM flight f WHERE f.id = recommendation.flight_id) IS NOT NULL
  AND airframe_id IS NOT (SELECT f.airframe_id FROM flight f WHERE f.id = recommendation.flight_id);
