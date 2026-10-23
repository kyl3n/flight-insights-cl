"""The column cache: decoded messages as encrypted Parquet, one file per flight and
message type (spec: Storage, "Encryption at rest").

Files use Parquet modular encryption. Every column is encrypted: position
columns (field metadata `position` other than "0") under the positions key,
everything else and the footer under the cache key. Reads leave position columns
out unless asked for them, and then decrypt with a key set that does not hold the
positions key, so analysis code cannot reach them by mistake.

Each file's data keys are wrapped in-process with AES-GCM under the master keys;
there is no key server. Decoded columns can always be rebuilt from the original
log, so evicting a flight's files loses nothing.
"""

from __future__ import annotations

import base64
import os
import re
import secrets
import shutil
from collections.abc import Iterable, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal, cast

import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.parquet.encryption as pe
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from flight_insights.store.keys import StoreKeys

CACHE_KEY = "cache"
POSITIONS_KEY = "positions"
FLIGHT_KEY = re.compile(r"[A-Za-z0-9_-]{1,128}")
NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
KEY_CACHE_LIFETIME = timedelta(minutes=10)
Compression = Literal["zstd", "snappy", "none"]


class CacheError(RuntimeError):
    """A cache file could not be written or read as asked."""


def is_position(field: pa.Field[Any]) -> bool:
    """Position columns are tagged at decode: "1", or "by_command" for MAVLink rows
    whose command decides. Either way the column goes under the positions key."""
    meta = cast(Mapping[bytes, bytes] | None, field.metadata) or {}
    return meta.get(b"position", b"0") not in (b"0", b"")


class _Kms(pe.KmsClient):
    """Wraps data keys with AES-GCM under in-memory master keys."""

    def __init__(self, masters: Mapping[str, bytes]) -> None:
        super().__init__()
        self._masters = dict(masters)

    def _key(self, key_id: str) -> AESGCM:
        if key_id not in self._masters:
            raise CacheError(f"the {key_id} key is not available to this read")
        return AESGCM(self._masters[key_id])

    def wrap_key(self, key_bytes: bytes, master_key_identifier: str) -> str:
        nonce = secrets.token_bytes(12)
        sealed = self._key(master_key_identifier).encrypt(
            nonce, key_bytes, master_key_identifier.encode()
        )
        return base64.b64encode(nonce + sealed).decode("ascii")

    # The stubs say str; pyarrow passes the returned bytes on as the key itself.
    def unwrap_key(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, wrapped_key: str, master_key_identifier: str
    ) -> bytes:
        raw = base64.b64decode(wrapped_key)
        try:
            return self._key(master_key_identifier).decrypt(
                raw[:12], raw[12:], master_key_identifier.encode()
            )
        except InvalidTag as e:
            raise CacheError(f"the {master_key_identifier} key does not open this file") from e


def _factory(masters: Mapping[str, bytes]) -> pe.CryptoFactory:
    return pe.CryptoFactory(lambda _config: _Kms(masters))


class ColumnCache:
    def __init__(self, root: Path, keys: StoreKeys, compression: Compression = "zstd") -> None:
        self.root = root
        self.compression: Compression = compression
        self._kms = pe.KmsConnectionConfig()
        self._all = _factory({CACHE_KEY: keys.cache, POSITIONS_KEY: keys.positions})
        self._analysis = _factory({CACHE_KEY: keys.cache})

    def path(self, flight_key: str, name: str) -> Path:
        if not FLIGHT_KEY.fullmatch(flight_key) or not NAME.fullmatch(name):
            raise CacheError(f"not a cache file name: {flight_key!r}/{name!r}")
        return self.root / flight_key / f"{name}.parquet"

    def exists(self, flight_key: str, name: str) -> bool:
        return self.path(flight_key, name).is_file()

    def names(self, flight_key: str) -> list[str]:
        folder = self.path(flight_key, "x").parent
        return sorted(p.stem for p in folder.glob("*.parquet")) if folder.is_dir() else []

    def evict(self, flight_key: str) -> None:
        """Drop a flight's decoded columns; they are rebuilt from the log when needed."""
        shutil.rmtree(self.path(flight_key, "x").parent, ignore_errors=True)

    def write(
        self,
        flight_key: str,
        name: str,
        batches: Iterable[pa.RecordBatch],
        schema: pa.Schema | None = None,
    ) -> Path:
        """Stream batches into one encrypted file. All batches share one schema, which
        is the first batch's unless given. The file appears only once complete."""
        target = self.path(flight_key, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        it = iter(batches)
        first = next(it, None)
        if schema is None:
            if first is None:
                raise CacheError("no batches and no schema to write")
            schema = first.schema
        props = self._all.file_encryption_properties(self._kms, _encryption(schema))
        partial = target.with_name(f".{target.name}.{secrets.token_hex(4)}.partial")
        try:
            with pq.ParquetWriter(
                partial,
                schema,
                compression=self.compression,
                encryption_properties=props,
                # keep Arrow's list child names, so tables read back exactly as written
                use_compliant_nested_type=False,
            ) as writer:
                for batch in [first, *it] if first is not None else []:
                    if not batch.schema.equals(schema, check_metadata=True):
                        raise CacheError(f"{name}: batches with different schemas")
                    writer.write_batch(batch)
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        return target

    def write_table(self, flight_key: str, name: str, table: pa.Table) -> Path:
        return self.write(flight_key, name, table.to_batches(), table.schema)

    def schema(self, flight_key: str, name: str) -> pa.Schema:
        return self._open(flight_key, name, positions=False).schema_arrow

    def read(
        self,
        flight_key: str,
        name: str,
        columns: list[str] | None = None,
        positions: bool = False,
    ) -> pa.Table:
        """A cached table. Position columns are left out, and cannot be decrypted,
        unless `positions` is True: only on-laptop map and location work asks."""
        f = self._open(flight_key, name, positions)
        fields = schema_fields(f.schema_arrow)
        hidden = {fld.name for fld in fields if is_position(fld)} if not positions else set[str]()
        if columns is None:
            columns = [fld.name for fld in fields if fld.name not in hidden]
        elif blocked := sorted(set(columns) & hidden):
            raise CacheError(f"{name}: {', '.join(blocked)} are positions: needs positions=True")
        return f.read(columns=columns)  # pyright: ignore[reportUnknownMemberType]

    def _open(self, flight_key: str, name: str, positions: bool) -> pq.ParquetFile:
        path = self.path(flight_key, name)
        if not path.is_file():
            raise CacheError(f"no cached {name} for {flight_key}")
        factory = self._all if positions else self._analysis
        props = factory.file_decryption_properties(
            self._kms, pe.DecryptionConfiguration(cache_lifetime=KEY_CACHE_LIFETIME)
        )
        return pq.ParquetFile(path, decryption_properties=props)


def _encryption(schema: pa.Schema) -> pe.EncryptionConfiguration:
    """Every top-level column is listed: Parquet leaves unlisted columns in plain text."""
    keys: dict[str, str | list[str]] = {}
    for fld in schema_fields(schema):
        cast(
            list[str], keys.setdefault(POSITIONS_KEY if is_position(fld) else CACHE_KEY, [])
        ).append(fld.name)
    config: Any = pe.EncryptionConfiguration(
        footer_key=CACHE_KEY,
        column_keys=keys,
        plaintext_footer=False,
        double_wrapping=True,
        cache_lifetime=KEY_CACHE_LIFETIME,
    )
    return cast(pe.EncryptionConfiguration, config)


def schema_fields(schema: pa.Schema) -> list[pa.Field[Any]]:
    return cast("list[pa.Field[Any]]", list(schema))
