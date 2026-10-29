-- A maker's bench table for a motor and prop at a voltage, entered by a person for a
-- platform: thrust against electrical power per motor, throttle step by step. The
-- platform's current parts and a candidate set compare on the Range and speed screen
-- (performance/bench.py). Synced like the platform's other records.
CREATE TABLE bench_table (
    id TEXT PRIMARY KEY,
    created_hlc TEXT NOT NULL,
    updated_hlc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    laptop_id TEXT NOT NULL REFERENCES laptop (id),
    platform_id TEXT NOT NULL REFERENCES platform (id),
    role TEXT NOT NULL CHECK (role IN ('current', 'candidate', 'none')),
    label TEXT NOT NULL,           -- what people call it: "V605 KV210, 22x7.4"
    motor TEXT,                    -- maker and model
    kv REAL,
    prop_diameter_in REAL,
    prop_pitch_in REAL,
    volts REAL,                    -- the test's supply voltage
    motor_mass_g REAL,             -- one motor, as the maker gives it
    source TEXT,                   -- where the table came from: a sheet, a page, a bench
    ambient_c REAL,                -- the air temperature the bench ran in, as the maker gives it
    rows_json TEXT NOT NULL        -- [{throttle_pct, thrust_g, power_w}], throttle rising
);
CREATE INDEX bench_table_platform ON bench_table (platform_id);
