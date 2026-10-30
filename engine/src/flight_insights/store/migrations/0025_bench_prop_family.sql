-- The propeller family a bench table's propeller belongs to (knowledge/rules/props.json
-- "families") and its blade count, so its steps calibrate that family's theory
-- (performance/propfamily.py). Both optional: a table without a family calibrates none.
ALTER TABLE bench_table ADD COLUMN prop_family TEXT;
ALTER TABLE bench_table ADD COLUMN prop_blades INTEGER;
