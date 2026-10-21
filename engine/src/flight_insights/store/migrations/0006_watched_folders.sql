-- Watched save folders (spec: Ground station profiles; setup step 5, "Point the app
-- at each station's save folders"). The shell finds new files there and offers
-- them for import; nothing is stored until a person confirms. A folder belongs
-- to a station and to the laptop that watches it, since a path means something
-- only on that computer. A file someone chose not to import is remembered by its
-- hash, so it is not offered again.

CREATE TABLE station_folder (
    id TEXT PRIMARY KEY,
    created_hlc TEXT NOT NULL,
    updated_hlc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    laptop_id TEXT NOT NULL REFERENCES laptop (id),
    ground_station_id TEXT NOT NULL REFERENCES ground_station (id),
    path TEXT NOT NULL,
    software TEXT CHECK (software IN ('Mission Planner', 'QGroundControl', 'other')),
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX station_folder_station ON station_folder (ground_station_id);

CREATE TABLE ignored_file (
    sha256 TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    ignored_hlc TEXT NOT NULL
) WITHOUT ROWID;
