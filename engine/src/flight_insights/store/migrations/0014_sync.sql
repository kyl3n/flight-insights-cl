-- Sync between laptops (spec: Shared storage and sync). Nothing here leaves the laptop
-- as it is: the shell encrypts and signs every object before it goes.
--
-- Every change to a synced record is captured in the same transaction that makes it:
-- which records and fields changed since the last object was written (sync_pending),
-- and each field's clock (field_clock), so changes merge field by field. Objects to
-- send wait in the outbox with this laptop's next sequence number. Every object
-- applied, from any laptop, is in the ledger: applying one twice changes nothing.
-- Objects that fail a check are quarantined with the reason, never applied and never
-- deleted. When two laptops changed the same field before syncing, the later change
-- wins and both values are kept in sync_conflict for a person to look at. Records two
-- laptops created for the same thing (the same flight or file, imported twice) merge
-- into one; sync_alias says which id the other became.

CREATE TABLE sync_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_sequence INTEGER NOT NULL DEFAULT 1,
    seeded INTEGER NOT NULL DEFAULT 0  -- records from before sync were queued once
);
INSERT INTO sync_state (id) VALUES (1);

CREATE TABLE sync_pending (
    tbl TEXT NOT NULL,
    rid TEXT NOT NULL,  -- a record's id, or a link's key
    field TEXT NOT NULL,  -- '*' for a new record or link
    base_hlc TEXT,  -- the field's clock before its first change since the last object
    PRIMARY KEY (tbl, rid, field)
) WITHOUT ROWID;

CREATE TABLE field_clock (
    tbl TEXT NOT NULL,
    rid TEXT NOT NULL,
    field TEXT NOT NULL,
    hlc TEXT NOT NULL,  -- when the field's value was set
    base_hlc TEXT,  -- the clock of the value it replaced: what its writer had seen
    PRIMARY KEY (tbl, rid, field)
) WITHOUT ROWID;

CREATE TABLE sync_outbox (
    sequence INTEGER PRIMARY KEY,  -- this laptop's own, from 1, no gaps
    kind TEXT NOT NULL CHECK (kind IN ('records', 'source')),
    payload_json TEXT NOT NULL,
    created_hlc TEXT NOT NULL,
    object_key TEXT,  -- set once the shell has written the object
    sent_hlc TEXT
);

CREATE TABLE sync_ledger (
    object_key TEXT PRIMARY KEY,
    laptop_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    kind TEXT NOT NULL,
    applied_hlc TEXT NOT NULL,
    UNIQUE (laptop_id, sequence)
);

CREATE TABLE sync_quarantine (
    object_key TEXT PRIMARY KEY,
    laptop_id TEXT,
    sequence INTEGER,
    reason TEXT NOT NULL,  -- which check failed; never a value from the object
    at_hlc TEXT NOT NULL
);

CREATE TABLE sync_conflict (
    id INTEGER PRIMARY KEY,
    tbl TEXT NOT NULL,
    rid TEXT NOT NULL,
    field TEXT NOT NULL,
    kept_json TEXT NOT NULL,
    kept_hlc TEXT NOT NULL,
    other_json TEXT NOT NULL,
    other_hlc TEXT NOT NULL,
    at_hlc TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX sync_conflict_open ON sync_conflict (resolved, tbl, rid);

CREATE TABLE sync_alias (
    tbl TEXT NOT NULL,
    old_id TEXT NOT NULL,
    new_id TEXT NOT NULL,
    PRIMARY KEY (tbl, old_id)
) WITHOUT ROWID;

-- Where this laptop's copy of a source file is: the path it was imported from, or the
-- object it arrived in. Local only: a path says nothing to another laptop.
CREATE TABLE source_location (
    sha256 TEXT PRIMARY KEY,
    path TEXT,
    object_key TEXT,
    seen_hlc TEXT NOT NULL
);

-- A recommendation's finding is recomputed on every laptop, so its portable reference
-- is the flight and the finding's code; the local finding row is a convenience.
CREATE TABLE recommendation_new (
    id TEXT PRIMARY KEY,
    created_hlc TEXT NOT NULL,
    updated_hlc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    laptop_id TEXT NOT NULL REFERENCES laptop (id),
    finding_id TEXT,
    finding_code TEXT,
    airframe_id TEXT REFERENCES airframe (id),
    flight_id TEXT REFERENCES flight (id),
    param TEXT,
    current_value REAL,
    proposed_value REAL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'accepted', 'rejected', 'applied', 'superseded',
                          'confirmed', 'no effect', 'regressed')),
    graded_flight_id TEXT REFERENCES flight (id),
    detail_json TEXT NOT NULL DEFAULT '{}'
);
INSERT INTO recommendation_new (
    id, created_hlc, updated_hlc, version, laptop_id, finding_id, finding_code, airframe_id,
    flight_id, param, current_value, proposed_value, stage, status, graded_flight_id, detail_json
)
SELECT
    r.id, r.created_hlc, r.updated_hlc, r.version, r.laptop_id, r.finding_id,
    (SELECT f.code FROM finding f WHERE f.id = r.finding_id), r.airframe_id, r.flight_id,
    r.param, r.current_value, r.proposed_value, r.stage, r.status, r.graded_flight_id,
    r.detail_json
FROM recommendation r;
DROP TABLE recommendation;
ALTER TABLE recommendation_new RENAME TO recommendation;
CREATE INDEX recommendation_airframe ON recommendation (airframe_id, status);

-- An AI packet's references, the same on every laptop: what its analysis run was made
-- with (run_key), and each finding by its portable key in refs_json. run_id stays this
-- laptop's own and does not sync.
ALTER TABLE ai_packet ADD COLUMN run_key TEXT;
