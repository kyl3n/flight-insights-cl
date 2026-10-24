-- The AI layer's exchanges with Claude (spec: AI layer, "Request flow").
--
-- Tool results are packets too: each is checked twice and kept in ai_packet like the
-- analysis packet, under the packet that started the exchange. A packet also keeps
-- which finding and evidence each of its packet-local references (F1, E1) stands for,
-- so a reply citing them can be traced back to the measurements.
--
-- One ai_exchange row per request to Claude that a packet started: what came back,
-- what the local check made of it, and what it cost. The reply is kept exactly as
-- received; whether each recommendation survived validation is kept beside it.

CREATE TABLE ai_packet_new (
    id TEXT PRIMARY KEY,
    created_hlc TEXT NOT NULL,
    updated_hlc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    laptop_id TEXT NOT NULL REFERENCES laptop (id),
    flight_id TEXT REFERENCES flight (id),
    purpose TEXT NOT NULL CHECK (purpose IN ('analysis', 'chat', 'explain', 'tool')),
    schema_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    packet_json TEXT NOT NULL,
    engine_verdict TEXT NOT NULL CHECK (engine_verdict IN ('pass', 'refused')),
    engine_reasons_json TEXT NOT NULL DEFAULT '[]',
    shell_verdict TEXT CHECK (shell_verdict IN ('pass', 'refused')),
    shell_reasons_json TEXT NOT NULL DEFAULT '[]',
    sent INTEGER NOT NULL DEFAULT 0 CHECK (sent IN (0, 1)),
    sent_hlc TEXT,
    parent_id TEXT,  -- a tool result: the packet it answers
    run_id TEXT,  -- the analysis run the packet was built from
    refs_json TEXT NOT NULL DEFAULT '{}'  -- "F1" -> finding id, "E1" -> evidence id
);
INSERT INTO ai_packet_new (
    id, created_hlc, updated_hlc, version, laptop_id, flight_id, purpose, schema_id, sha256,
    packet_json, engine_verdict, engine_reasons_json, shell_verdict, shell_reasons_json, sent,
    sent_hlc
)
SELECT
    id, created_hlc, updated_hlc, version, laptop_id, flight_id, purpose, schema_id, sha256,
    packet_json, engine_verdict, engine_reasons_json, shell_verdict, shell_reasons_json, sent,
    sent_hlc
FROM ai_packet;
DROP TABLE ai_packet;
ALTER TABLE ai_packet_new RENAME TO ai_packet;
CREATE INDEX ai_packet_flight ON ai_packet (flight_id, created_hlc);
CREATE INDEX ai_packet_parent ON ai_packet (parent_id);

CREATE TABLE ai_exchange (
    id TEXT PRIMARY KEY,
    created_hlc TEXT NOT NULL,
    updated_hlc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    laptop_id TEXT NOT NULL REFERENCES laptop (id),
    packet_id TEXT NOT NULL REFERENCES ai_packet (id),
    flight_id TEXT REFERENCES flight (id),
    purpose TEXT NOT NULL,
    month TEXT NOT NULL,  -- UTC year and month, for the monthly spend cap
    model TEXT NOT NULL,
    stop_reason TEXT,
    -- accepted: the reply fits and cites only what the packet holds; invalid: it does
    -- not; declined: Claude declined; failed: no usable reply (cut off, network, API)
    outcome TEXT NOT NULL CHECK (outcome IN ('accepted', 'invalid', 'declined', 'failed')),
    reasons_json TEXT NOT NULL DEFAULT '[]',
    reply_json TEXT,
    recommendations_json TEXT NOT NULL DEFAULT '[]',  -- each one's validation verdict
    tool_calls INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0
);
CREATE INDEX ai_exchange_flight ON ai_exchange (flight_id, created_hlc);
CREATE INDEX ai_exchange_month ON ai_exchange (month);
