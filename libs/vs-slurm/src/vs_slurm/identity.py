"""Deterministic identity of the Slurm execution runtime."""

from __future__ import annotations

import hashlib
from pathlib import Path

_RUNTIME_FILES = (
    "cluster.py",
    "cluster_store.py",
    "cluster_types.py",
    "config.py",
    "runner.py",
    "staging.py",
)


def runtime_content_identity() -> str:
    """Return a content digest for code that defines remote job semantics."""
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for name in _RUNTIME_FILES:
        content = (root / name).read_bytes()
        encoded_name = name.encode()
        digest.update(len(encoded_name).to_bytes(8, "big"))
        digest.update(encoded_name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


__all__ = ["runtime_content_identity"]
