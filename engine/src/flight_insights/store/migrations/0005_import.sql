-- Import (spec: Platforms, "Every import asks which airframe and what changed").
-- A flight's natural key makes a second import of the same flight, from the same
-- file or another copy of it, find the first. A flight waiting in the hold queue
-- has no airframe yet; hold_json says why and which airframes it might be.
-- hardware_json keeps the device IDs, CAN nodes and firmware it flew with, so the
-- next flight's import can list what changed.

ALTER TABLE flight ADD COLUMN flight_key TEXT;
ALTER TABLE flight ADD COLUMN board_id_hash TEXT;
ALTER TABLE flight ADD COLUMN arm_us INTEGER;
ALTER TABLE flight ADD COLUMN disarm_us INTEGER;
ALTER TABLE flight ADD COLUMN boot_utc_us INTEGER;
ALTER TABLE flight ADD COLUMN hardware_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE flight ADD COLUMN hold_json TEXT;
ALTER TABLE flight ADD COLUMN payload TEXT;

CREATE UNIQUE INDEX flight_natural_key ON flight (flight_key);
CREATE INDEX flight_airframe ON flight (airframe_id, boot_utc_us);

-- What a source file holds beyond its bytes, such as the vehicle boots a tlog
-- covers, so a tlog imported before its flight is paired when the flight arrives.
ALTER TABLE source_file ADD COLUMN meta_json TEXT NOT NULL DEFAULT '{}';

-- A parameter file's snapshot belongs to the airframe it was matched to; a log's
-- snapshot is reached through its flights.
ALTER TABLE param_snapshot ADD COLUMN airframe_id TEXT REFERENCES airframe (id);
