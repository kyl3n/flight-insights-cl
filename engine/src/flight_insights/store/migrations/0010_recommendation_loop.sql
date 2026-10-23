-- Closing the loop (spec: Recommendation engine, "Closing the loop").
--
-- A recommendation remembers the flight it was proposed from and, once graded, the
-- later flight of the same airframe that graded it: confirmed, no effect or
-- regressed. A proposal a later plan replaced before anyone acted on it is
-- superseded. Its detail keeps what the person saw (why, evidence, expected effect,
-- verification) and the grade's own evidence. SQLite cannot change a CHECK in
-- place, so the table is rebuilt with its rows.

CREATE TABLE recommendation_new (
    id TEXT PRIMARY KEY,
    created_hlc TEXT NOT NULL,
    updated_hlc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    laptop_id TEXT NOT NULL REFERENCES laptop (id),
    finding_id TEXT NOT NULL REFERENCES finding (id),
    airframe_id TEXT REFERENCES airframe (id),
    flight_id TEXT REFERENCES flight (id),  -- the flight whose plan proposed it
    param TEXT,  -- canonical name; null for a hardware or inspection action
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
    id, created_hlc, updated_hlc, version, laptop_id, finding_id, airframe_id, param,
    current_value, proposed_value, stage, status, detail_json
)
SELECT
    id, created_hlc, updated_hlc, version, laptop_id, finding_id, airframe_id, param,
    current_value, proposed_value, stage, status, detail_json
FROM recommendation;
DROP TABLE recommendation;
ALTER TABLE recommendation_new RENAME TO recommendation;
CREATE INDEX recommendation_airframe ON recommendation (airframe_id, status);
