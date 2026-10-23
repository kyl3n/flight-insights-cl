-- The firmware's default for each value, as the log's PARM messages record it (spec:
-- Analyzers, "Parameter audit"). A value equal to its default was never chosen for this
-- vehicle. Null where the source does not say: parameter files, older firmware.

ALTER TABLE param_value ADD COLUMN default_value REAL;
