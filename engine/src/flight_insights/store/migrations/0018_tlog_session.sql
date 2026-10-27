-- Each flight's tlog sessions: the boot of the vehicle a tlog recorded that is this
-- flight's, with the station's clock fitted against the vehicle's and the packets lost
-- on the way down (spec: Link and ground-station analysis, "Pairing and alignment").
-- Derived from the tlog and the onboard log, so each laptop computes its own, like
-- analysis results: never synced.

CREATE TABLE tlog_session (
    id TEXT PRIMARY KEY,
    created_hlc TEXT NOT NULL,
    updated_hlc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    laptop_id TEXT NOT NULL REFERENCES laptop (id),
    flight_id TEXT NOT NULL REFERENCES flight (id),
    source_file_id TEXT NOT NULL REFERENCES source_file (id),
    sysid INTEGER NOT NULL,
    compid INTEGER NOT NULL,
    -- 'aligned' (the attitudes agree at zero lag), 'corrected' (they agree at another
    -- lag, applied), 'unconfirmed' (no attitude to check against), 'no clock' (too few
    -- timed packets), 'missing' (the tlog cannot be read on this laptop)
    status TEXT NOT NULL CHECK (
        status IN ('aligned', 'corrected', 'unconfirmed', 'no clock', 'missing')
    ),
    -- station clock = offset_us + (1 + drift_ppm / 1e6) * vehicle boot clock, in µs
    offset_us INTEGER,
    drift_ppm REAL,
    first_record INTEGER,  -- the boot's first and last packet in the tlog
    last_record INTEGER,
    received INTEGER NOT NULL DEFAULT 0,
    lost INTEGER NOT NULL DEFAULT 0,
    out_of_order INTEGER NOT NULL DEFAULT 0,
    quality_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (flight_id, source_file_id, sysid, compid)
);

CREATE INDEX tlog_session_flight ON tlog_session (flight_id);
