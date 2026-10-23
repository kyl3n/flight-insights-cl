-- The parameter changes a finding proposes (spec: Analyzers common to all vehicles: "a
-- finding carries ... zero or more candidate parameter changes"). Kept with the
-- finding as the analyzer emitted them, canonical names, for the recommendation engine
-- to validate, order and turn into recommendations. Nothing here is ever applied.

ALTER TABLE finding ADD COLUMN changes_json TEXT NOT NULL DEFAULT '[]';
