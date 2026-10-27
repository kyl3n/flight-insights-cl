-- Each flight's weather record (spec: Weather and wind, "The weather record per
-- flight"): values only, worked out on the laptop from the regional downloads and the
-- flight's own track, never a position. Derived, like analysis results: never synced.

CREATE TABLE flight_weather (
    flight_id TEXT PRIMARY KEY REFERENCES flight (id),
    -- the downloads it was made from, with when each was fetched, and the rules: made
    -- again when any of them changes (reanalysis arriving after the forecast)
    basis TEXT NOT NULL,
    record_json TEXT NOT NULL
);
