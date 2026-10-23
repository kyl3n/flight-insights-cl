-- A flight's log checks (spec: Data ingestion, step 4 "Validate").
--
-- Worked out once, at import, while the log is open, like the fingerprint: the
-- logging dropouts, timestamp gaps, raw-IMU sample loss and IMU error counts that
-- overlap the flight. The logging-adequacy analyzer reads them; filter analysis is
-- gated on them.

ALTER TABLE flight ADD COLUMN validation_json TEXT;
