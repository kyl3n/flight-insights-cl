-- A flight's take-off weight as weighed before it flew, entered at import (spec:
-- Performance trends and ideal ranges, "Like with like"). One weighed flight turns an
-- airframe's payload bands into kilograms; two or more check the hover-thrust estimate
-- against the scale. Synced with the rest of the flight record.

ALTER TABLE flight ADD COLUMN takeoff_kg REAL CHECK (takeoff_kg IS NULL OR takeoff_kg > 0);
