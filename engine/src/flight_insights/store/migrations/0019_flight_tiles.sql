-- The weather tiles each flight flew in (spec: Weather and wind, "How the location
-- stays private"): tile names only, worked out on the laptop from the flight's
-- positions, never a position. Derived from the log, so each laptop finds its own,
-- like analysis results: never synced.

CREATE TABLE flight_tiles (
    flight_id TEXT PRIMARY KEY REFERENCES flight (id),
    -- the rules the tiles were found with (rules/weather.json); found again on a change
    basis TEXT NOT NULL,
    -- JSON list of tile names, "N50E030"; empty when the flight logged no position
    tiles TEXT NOT NULL
);
