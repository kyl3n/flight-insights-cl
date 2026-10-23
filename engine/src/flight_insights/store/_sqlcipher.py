"""A typed doorway to sqlcipher3, which ships no type information.

Its connections behave exactly as the standard library's sqlite3 ones, so they
are typed as those.
"""

from __future__ import annotations

import sqlite3
from typing import Any, cast

import sqlcipher3  # pyright: ignore[reportMissingTypeStubs]

_module = cast(Any, sqlcipher3)
DatabaseError = cast(type[Exception], _module.DatabaseError)
IntegrityError = cast(type[Exception], _module.IntegrityError)


def connect(path: str) -> sqlite3.Connection:
    # isolation_level=None: the store opens and closes its own transactions.
    return cast(sqlite3.Connection, _module.connect(path, isolation_level=None))
