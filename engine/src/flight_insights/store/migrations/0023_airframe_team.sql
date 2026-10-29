-- The team (operator) that flies an airframe: aircraft of one platform can belong to
-- different teams, and the fleet, trends and fleet evidence can say which. Synced like
-- the airframe's other fields; NULL until someone names it.
ALTER TABLE airframe ADD COLUMN team TEXT;
CREATE INDEX airframe_team ON airframe (team);
