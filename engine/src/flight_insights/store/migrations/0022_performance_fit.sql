-- Each airframe's power model (spec: Power and efficiency analysis, "Range, endurance and
-- speed", "The power model"): the points from its flights and the fit, made on this laptop
-- from the logs and kept until what it was made from changes. Derived, like analysis
-- results: never synced.

CREATE TABLE performance_fit (
    airframe_id TEXT PRIMARY KEY REFERENCES airframe (id),
    -- what it was made from: the engine and knowledge, the airframe and its setup, and
    -- each flight with its latest analysis and weather record
    basis TEXT NOT NULL,
    model_json TEXT NOT NULL
);
