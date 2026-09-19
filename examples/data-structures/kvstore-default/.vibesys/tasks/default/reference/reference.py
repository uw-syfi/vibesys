from __future__ import annotations

import threading


class ReferenceKVStore:
    def __init__(self):
        self._data: dict[str, int] = {}
        self._lock = threading.Lock()

    def put(self, key: str, value: int) -> bool:
        with self._lock:
            self._data[key] = value
        return True

    def get(self, key: str):
        with self._lock:
            return self._data.get(key)

    def delete(self, key: str) -> bool:
        with self._lock:
            existed = key in self._data
            if existed:
                del self._data[key]
        return existed


def KVStoreFactory():
    return ReferenceKVStore()
