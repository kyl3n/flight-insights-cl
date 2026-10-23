"""The keys that unlock the local store (spec: Storage, "Encryption at rest").

The shell reads them from the OS keychain and hands them to the engine at
start-up. The engine keeps them in memory only: they are never written to disk,
logged or shown, and their repr hides them.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field

KEY_BYTES = 32
LAPTOP_ID = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class StoreKeys:
    database: bytes = field(repr=False)  # SQLCipher key for the entity database
    cache: bytes = field(repr=False)  # Parquet cache: footers and every non-position column
    positions: bytes = field(repr=False)  # Parquet cache: position columns only

    def __post_init__(self) -> None:
        keys = (self.database, self.cache, self.positions)
        if any(len(k) != KEY_BYTES for k in keys):
            raise ValueError(f"store keys must be {KEY_BYTES} bytes each")
        if len(set(keys)) != len(keys):
            raise ValueError("store keys must differ from each other")

    @classmethod
    def generate(cls) -> StoreKeys:
        """Fresh random keys, for tests and throwaway stores. The app's keys come from the shell."""
        return cls(*(secrets.token_bytes(KEY_BYTES) for _ in range(3)))

    @classmethod
    def from_hex(cls, database: str, cache: str, positions: str) -> StoreKeys:
        return cls(bytes.fromhex(database), bytes.fromhex(cache), bytes.fromhex(positions))


def check_laptop_id(laptop_id: str) -> str:
    """A laptop ID is a random 128-bit value, written as 32 lower-case hex digits."""
    if not LAPTOP_ID.fullmatch(laptop_id):
        raise ValueError("a laptop ID is 32 lower-case hex digits")
    return laptop_id


def short_code(laptop_id: str) -> str:
    """The short form people see next to the laptop's name, such as LT-3F9A2C."""
    return "LT-" + check_laptop_id(laptop_id)[:6].upper()
