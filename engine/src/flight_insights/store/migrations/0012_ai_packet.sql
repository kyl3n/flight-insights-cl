-- Every AI packet, as it was built and judged (spec: AI layer, "Visible to the user":
-- every packet sent is stored locally and viewable as "what left this machine").
--
-- The packet is kept exactly as checked, with both verdicts: the engine's scrubber and
-- the shell's separate check, which runs just before sending. A packet either check
-- refuses is kept too, with its reasons (paths and rules, never the values), so a
-- refusal can be looked into. The flight's own positions (the tripwire) are never kept.
-- This is the laptop's own record: it says what left this machine.

CREATE TABLE ai_packet (
    id TEXT PRIMARY KEY,
    created_hlc TEXT NOT NULL,
    updated_hlc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    laptop_id TEXT NOT NULL REFERENCES laptop (id),
    flight_id TEXT REFERENCES flight (id),
    purpose TEXT NOT NULL CHECK (purpose IN ('analysis', 'chat', 'explain')),
    schema_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,  -- of packet_json, exactly as checked and as sent
    packet_json TEXT NOT NULL,
    engine_verdict TEXT NOT NULL CHECK (engine_verdict IN ('pass', 'refused')),
    engine_reasons_json TEXT NOT NULL DEFAULT '[]',
    shell_verdict TEXT CHECK (shell_verdict IN ('pass', 'refused')),
    shell_reasons_json TEXT NOT NULL DEFAULT '[]',
    sent INTEGER NOT NULL DEFAULT 0 CHECK (sent IN (0, 1)),
    sent_hlc TEXT
);
CREATE INDEX ai_packet_flight ON ai_packet (flight_id, created_hlc);
