-- Parameter history: when a snapshot applies from, and the changes made while the
-- same log kept recording (spec: Parameter history).

ALTER TABLE param_snapshot ADD COLUMN taken_us INTEGER;  -- log time the set applies from

CREATE TABLE param_change (
    snapshot_id TEXT NOT NULL REFERENCES param_snapshot (id),
    seq INTEGER NOT NULL,  -- order within the log
    time_us INTEGER NOT NULL,  -- log time
    name TEXT NOT NULL,
    old_value REAL NOT NULL,
    new_value REAL NOT NULL,
    PRIMARY KEY (snapshot_id, seq)
) WITHOUT ROWID;
