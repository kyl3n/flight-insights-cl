-- Phase 0 entities (spec: Storage, data model and fleet history).
--
-- Every record carries what sync needs: a random 128-bit id, the hybrid logical
-- clock stamps of its creation and latest change, the version a change was based
-- on, and the laptop that wrote it. Child rows that only exist as part of an
-- immutable parent (parameter values of a snapshot, links) carry the parent's
-- identity instead. Board unique IDs are stored only as keyed hashes.

CREATE TABLE laptop (
    id TEXT PRIMARY KEY,  -- the laptop ID itself
    created_hlc TEXT NOT NULL,
    updated_hlc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    laptop_id TEXT NOT NULL REFERENCES laptop (id),
    name TEXT NOT NULL,
    signing_public_key BLOB,
    encryption_public_key BLOB,
    enrolled_hlc TEXT,
    revoked_hlc TEXT
);

CREATE TABLE platform (
    id TEXT PRIMARY KEY,
    created_hlc TEXT NOT NULL,
    updated_hlc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    laptop_id TEXT NOT NULL REFERENCES laptop (id),
    name TEXT NOT NULL,
    vehicle TEXT NOT NULL CHECK (vehicle IN ('copter', 'plane')),
    reference_build_id TEXT REFERENCES build_version (id),
    profile_json TEXT NOT NULL DEFAULT '{}',  -- answers to the configuration questions
    targets_json TEXT NOT NULL DEFAULT '{}'  -- targets for ideal ranges
);

CREATE TABLE airframe (
    id TEXT PRIMARY KEY,
    created_hlc TEXT NOT NULL,
    updated_hlc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    laptop_id TEXT NOT NULL REFERENCES laptop (id),
    name TEXT NOT NULL,
    platform_id TEXT NOT NULL REFERENCES platform (id),
    archetype TEXT,
    current_build_id TEXT REFERENCES build_version (id),
    tuning_stage TEXT,
    board_id_hash TEXT,  -- keyed hash of the board's unique ID; the raw ID is never stored
    overrides_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX airframe_board ON airframe (board_id_hash);

CREATE TABLE build_version (
    id TEXT PRIMARY KEY,
    created_hlc TEXT NOT NULL,
    updated_hlc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    laptop_id TEXT NOT NULL REFERENCES laptop (id),
    -- a platform's reference build, or a build of one airframe
    platform_id TEXT REFERENCES platform (id),
    airframe_id TEXT REFERENCES airframe (id),
    number INTEGER NOT NULL,
    based_on_id TEXT REFERENCES build_version (id),
    effective_date TEXT,
    first_flight_id TEXT REFERENCES flight (id),
    reason TEXT,
    CHECK ((platform_id IS NULL) != (airframe_id IS NULL))
);

CREATE TABLE component (
    id TEXT PRIMARY KEY,
    created_hlc TEXT NOT NULL,
    updated_hlc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    laptop_id TEXT NOT NULL REFERENCES laptop (id),
    type TEXT NOT NULL,
    make TEXT,
    model TEXT,
    firmware TEXT,
    specs_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE build_component (
    build_version_id TEXT NOT NULL REFERENCES build_version (id),
    component_id TEXT NOT NULL REFERENCES component (id),
    position TEXT NOT NULL DEFAULT '',  -- where on the airframe, such as "motor 3"
    install_date TEXT,
    PRIMARY KEY (build_version_id, component_id, position)
) WITHOUT ROWID;

CREATE TABLE battery_pack (
    id TEXT PRIMARY KEY,
    created_hlc TEXT NOT NULL,
    updated_hlc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    laptop_id TEXT NOT NULL REFERENCES laptop (id),
    pack_code TEXT NOT NULL,
    chemistry TEXT,
    cells INTEGER,
    capacity_mah REAL,
    cycles INTEGER
);

CREATE TABLE ground_station (
    id TEXT PRIMARY KEY,
    created_hlc TEXT NOT NULL,
    updated_hlc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    laptop_id TEXT NOT NULL REFERENCES laptop (id),
    name TEXT NOT NULL,
    current_version_id TEXT REFERENCES ground_station_version (id)
);

CREATE TABLE ground_station_version (
    id TEXT PRIMARY KEY,
    created_hlc TEXT NOT NULL,
    updated_hlc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    laptop_id TEXT NOT NULL REFERENCES laptop (id),
    ground_station_id TEXT NOT NULL REFERENCES ground_station (id),
    number INTEGER NOT NULL,
    effective_date TEXT,
    computer_json TEXT NOT NULL DEFAULT '{}',
    software_json TEXT NOT NULL DEFAULT '{}',
    radios_json TEXT NOT NULL DEFAULT '[]',
    antennas_json TEXT NOT NULL DEFAULT '[]',
    reason TEXT
);

CREATE TABLE source_file (
    id TEXT PRIMARY KEY,
    created_hlc TEXT NOT NULL,
    updated_hlc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    laptop_id TEXT NOT NULL REFERENCES laptop (id),
    sha256 TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('dataflash', 'tlog', 'param', 'params')),
    size INTEGER NOT NULL,
    original_name TEXT NOT NULL,
    ground_station_version_id TEXT REFERENCES ground_station_version (id)
);

CREATE TABLE flight (
    id TEXT PRIMARY KEY,
    created_hlc TEXT NOT NULL,
    updated_hlc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    laptop_id TEXT NOT NULL REFERENCES laptop (id),
    airframe_id TEXT REFERENCES airframe (id),  -- null while the flight waits in the hold queue
    build_version_id TEXT REFERENCES build_version (id),
    ground_station_version_id TEXT REFERENCES ground_station_version (id),
    param_snapshot_id TEXT REFERENCES param_snapshot (id),
    firmware TEXT,
    log_start_us INTEGER,  -- natural identity, with the airframe, boot and firmware
    boot_count INTEGER,
    duration_s REAL,
    quality_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE flight_source (
    flight_id TEXT NOT NULL REFERENCES flight (id),
    source_file_id TEXT NOT NULL REFERENCES source_file (id),
    role TEXT NOT NULL CHECK (role IN ('dataflash', 'tlog')),
    PRIMARY KEY (flight_id, source_file_id)
) WITHOUT ROWID;

CREATE TABLE flight_battery (
    flight_id TEXT NOT NULL REFERENCES flight (id),
    battery_pack_id TEXT NOT NULL REFERENCES battery_pack (id),
    PRIMARY KEY (flight_id, battery_pack_id)
) WITHOUT ROWID;

CREATE TABLE param_snapshot (
    id TEXT PRIMARY KEY,
    created_hlc TEXT NOT NULL,
    updated_hlc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    laptop_id TEXT NOT NULL REFERENCES laptop (id),
    set_id TEXT NOT NULL,  -- hash of the canonical set: equal IDs flew identical configurations
    vehicle TEXT NOT NULL CHECK (vehicle IN ('copter', 'plane')),
    firmware TEXT,  -- the dialect the values are stored in
    source_file_id TEXT REFERENCES source_file (id),
    origin TEXT NOT NULL CHECK (origin IN ('log start', 'tlog download', 'file')),
    confidence TEXT NOT NULL CHECK (confidence IN ('primary', 'fallback'))
);
CREATE INDEX param_snapshot_set ON param_snapshot (set_id);

CREATE TABLE param_value (
    snapshot_id TEXT NOT NULL REFERENCES param_snapshot (id),
    name TEXT NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (snapshot_id, name)
) WITHOUT ROWID;

CREATE TABLE change_set (
    id TEXT PRIMARY KEY,
    created_hlc TEXT NOT NULL,
    updated_hlc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    laptop_id TEXT NOT NULL REFERENCES laptop (id),
    airframe_id TEXT NOT NULL REFERENCES airframe (id),
    kind TEXT NOT NULL CHECK (kind IN ('parameter', 'build', 'ground station')),
    origin TEXT NOT NULL CHECK (origin IN (
        'recommendation', 'ground station edit', 'loaded file', 'in flight',
        'firmware conversion', 'unexplained'
    )),
    before_flight_id TEXT REFERENCES flight (id),
    after_flight_id TEXT REFERENCES flight (id),
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX change_set_airframe ON change_set (airframe_id);
