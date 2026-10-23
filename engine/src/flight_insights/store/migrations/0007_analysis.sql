-- Analysis (spec: Analyzers common to all vehicles; Storage, data model).
--
-- A flight's fingerprint is worked out once, at import, while the log is open; the
-- analyzers then read only the flight's cached columns, where positions stay
-- encrypted under their own key, and this fingerprint.
--
-- An analysis run records the engine and knowledge versions it ran with, so its
-- results are reused while those match and recomputed when they change. Metric
-- values and findings belong to their run and are never edited: a new run adds
-- rows. A recommendation is a proposed change; its status changes as the user
-- accepts, applies and verifies it.

ALTER TABLE flight ADD COLUMN fingerprint_json TEXT;

CREATE TABLE analysis_run (
    id TEXT PRIMARY KEY,
    created_hlc TEXT NOT NULL,
    updated_hlc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    laptop_id TEXT NOT NULL REFERENCES laptop (id),
    flight_id TEXT NOT NULL REFERENCES flight (id),
    engine_version TEXT NOT NULL,
    knowledge_version TEXT NOT NULL,
    analyzers_json TEXT NOT NULL,  -- each analyzer's name and version, and what it skipped
    evidence_json TEXT NOT NULL DEFAULT '[]',  -- everything its metrics and findings cite
    status TEXT NOT NULL CHECK (status IN ('complete', 'partial'))
);
CREATE INDEX analysis_run_flight ON analysis_run (flight_id, created_hlc);

CREATE TABLE metric_value (
    id TEXT PRIMARY KEY,
    created_hlc TEXT NOT NULL,
    updated_hlc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    laptop_id TEXT NOT NULL REFERENCES laptop (id),
    run_id TEXT NOT NULL REFERENCES analysis_run (id),
    flight_id TEXT NOT NULL REFERENCES flight (id),
    metric TEXT NOT NULL,  -- an id in the knowledge pack's metric registry
    segment TEXT NOT NULL,  -- 'flight', 'hover' ...
    value REAL NOT NULL,
    spread_json TEXT NOT NULL DEFAULT '{}',
    zone TEXT CHECK (zone IN ('target', 'caution', 'out')),
    evidence_id TEXT
);
CREATE INDEX metric_value_metric ON metric_value (metric, flight_id);

CREATE TABLE finding (
    id TEXT PRIMARY KEY,
    created_hlc TEXT NOT NULL,
    updated_hlc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    laptop_id TEXT NOT NULL REFERENCES laptop (id),
    run_id TEXT NOT NULL REFERENCES analysis_run (id),
    flight_id TEXT NOT NULL REFERENCES flight (id),
    analyzer TEXT NOT NULL,
    code TEXT NOT NULL,  -- an id in the registry's findings, which holds its wording
    severity TEXT NOT NULL CHECK (severity IN ('info', 'caution', 'out', 'safety')),
    confidence TEXT NOT NULL CHECK (confidence IN ('low', 'medium', 'high')),
    params_json TEXT NOT NULL DEFAULT '{}',  -- the numbers the wording fills in
    evidence_json TEXT NOT NULL  -- never empty: every finding shows what it rests on
);
CREATE INDEX finding_flight ON finding (flight_id, run_id);

CREATE TABLE recommendation (
    id TEXT PRIMARY KEY,
    created_hlc TEXT NOT NULL,
    updated_hlc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    laptop_id TEXT NOT NULL REFERENCES laptop (id),
    finding_id TEXT NOT NULL REFERENCES finding (id),
    airframe_id TEXT REFERENCES airframe (id),
    param TEXT,  -- canonical name; null for a hardware or inspection action
    current_value REAL,
    proposed_value REAL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'accepted', 'rejected', 'applied', 'confirmed', 'no effect', 'regressed')),
    detail_json TEXT NOT NULL DEFAULT '{}'
);
