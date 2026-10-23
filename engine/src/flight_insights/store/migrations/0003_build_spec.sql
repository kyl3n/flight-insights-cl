-- The setup answers of a build (spec: Platforms, "Setting up a new platform").
-- A platform's reference build holds every answer; an airframe's build holds only
-- where it differs from that reference. Each value records whether a person
-- entered it or the engine detected it, and from what.

ALTER TABLE build_version ADD COLUMN spec_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE build_version ADD COLUMN reason_kind TEXT
    CHECK (reason_kind IN ('setup', 'failure', 'upgrade', 'test', 'detected'));
