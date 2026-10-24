-- Which board-ID key the stored hashes were made with, as a keyed check value (never
-- the key): when a laptop joins a fleet its hashes are remade with the fleet's key, so
-- the same board is recognised on every laptop (spec: Shared storage and sync).
ALTER TABLE sync_state ADD COLUMN board_check TEXT;
