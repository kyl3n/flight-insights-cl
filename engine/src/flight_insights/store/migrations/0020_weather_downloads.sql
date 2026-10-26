-- The weather the shell downloaded (spec: Weather and wind, "Fixed schedule"), kept as
-- it came, compressed, inside the encrypted store until each flight's weather record
-- is made from it; and when this laptop first listed each tile, so a new tile's
-- backfill waits its random delay across restarts. Both are this laptop's own: shared
-- downloads travel as encrypted drive objects, not as synced records.

CREATE TABLE weather_tile (
    tile TEXT PRIMARY KEY,  -- "N50E030"
    added_ms INTEGER NOT NULL
);

CREATE TABLE weather_download (
    source TEXT NOT NULL CHECK (source IN ('forecast', 'reanalysis', 'kp')),
    tile TEXT NOT NULL,  -- '' for space weather
    -- UTC: a month '2026-10', a day '2026-10-20' or a 6-hour slot '2026-10-26T06'
    period TEXT NOT NULL,
    fetched_ms INTEGER NOT NULL,
    body BLOB NOT NULL,  -- the reply as it came, gzip
    PRIMARY KEY (source, tile, period)
);
