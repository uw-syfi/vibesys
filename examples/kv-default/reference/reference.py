"""Reference KV store implementations for VibeServe KV scenarios.

Each class satisfies the shared API from issue #26:
    put(key, value) -> bool
    get(key)        -> bytes | None
    delete(key)     -> bool
    size()          -> int
    stats()         -> dict

Keys are UTF-8 strings. Values are opaque bytes.
A successful put makes the value visible to later get calls for the same key.
A successful delete removes the key for later get calls.
Failed operations must not be reported as successful.
"""

from __future__ import annotations
import threading
from typing import Optional


class _BaseKVStore:
    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}
        self._write_lock = threading.Lock()
        self._ops = {"put": 0, "get": 0, "delete": 0, "hit": 0, "miss": 0}

    def put(self, key: str, value: bytes) -> bool:
        if not isinstance(key, str) or not isinstance(value, (bytes, bytearray)):
            return False
        with self._write_lock:
            self._store[key] = bytes(value)
            self._ops["put"] += 1
        return True

    def get(self, key: str) -> Optional[bytes]:
        if not isinstance(key, str):
            return None
        with self._write_lock:
            result = self._store.get(key)
            self._ops["get"] += 1
            if result is not None:
                self._ops["hit"] += 1
            else:
                self._ops["miss"] += 1
        return result

    def delete(self, key: str) -> bool:
        if not isinstance(key, str):
            return False
        with self._write_lock:
            existed = key in self._store
            if existed:
                del self._store[key]
            self._ops["delete"] += 1
        return existed

    def size(self) -> int:
        with self._write_lock:
            return len(self._store)

    def stats(self) -> dict:
        with self._write_lock:
            return {**self._ops, "size": len(self._store)}


class PointKVStore(_BaseKVStore):
    pass

class ScanKVStore(_BaseKVStore):
    def scan(self, prefix: str) -> list[tuple[str, bytes]]:
        with self._write_lock:
            return [(k, v) for k, v in sorted(self._store.items()) if k.startswith(prefix)]

class HeavyWriteKVStore(_BaseKVStore):
    pass

class ReadHeavyKVStore(_BaseKVStore):
    pass


SCENARIOS = ["point", "scan", "heavy-write", "read-heavy"]
_SCENARIO_MAP = {
    "point":       PointKVStore,
    "scan":        ScanKVStore,
    "heavy-write": HeavyWriteKVStore,
    "read-heavy":  ReadHeavyKVStore,
}

def KVFactory(scenario: str) -> _BaseKVStore:
    cls = _SCENARIO_MAP.get(scenario)
    if cls is None:
        raise ValueError(f"Unknown scenario {scenario!r}. Choose from {SCENARIOS}")
    return cls()
