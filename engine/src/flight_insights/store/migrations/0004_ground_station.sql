-- Ground station profiles (spec: Ground station profiles). A version holds every
-- part of the profile, each answer with where it came from. The computer's host
-- name is kept only as a keyed hash, enough to know the app runs on that computer.

ALTER TABLE ground_station_version ADD COLUMN connections_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE ground_station_version ADD COLUMN clock_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE ground_station_version ADD COLUMN host_hash TEXT;
ALTER TABLE ground_station_version ADD COLUMN based_on_id TEXT
    REFERENCES ground_station_version (id);
ALTER TABLE ground_station_version ADD COLUMN reason_kind TEXT
    CHECK (reason_kind IN ('setup', 'failure', 'upgrade', 'test', 'detected'));

CREATE INDEX ground_station_version_host ON ground_station_version (host_hash);
