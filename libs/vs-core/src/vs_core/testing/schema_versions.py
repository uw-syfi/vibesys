"""Check a persisted model's JSON schema against the hash pinned for its version.

A golden file lists, one line per version, the version and the SHA-256 of the persisted
shape's JSON schema (``<version> <hex digest>``; lines starting with ``#`` are comments).
A version is one number, or several joined by dots when the shape depends on several
version numbers (``9.1``: a record of version 1 around an envelope of version 9). The
current shape must be the one pinned on the last line, for the current version. Earlier
lines cannot be recomputed from one checkout, so the file is append-only: its header
carries the ``# append-only`` marker, and ``scripts/check_stale_revert.py`` fails a change
that edits or drops a line the base branch already had.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


def schema_digest(schema: Mapping[str, object]) -> str:
    """The SHA-256 of ``schema`` in canonical JSON (sorted keys, no spaces)."""
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def assert_schema_pinned(
    golden: str, version: tuple[int, ...], schema: Mapping[str, object]
) -> None:
    """Fail unless the golden text ends with ``version`` pinned to ``schema``, versions ascending.

    The failure names the line to append: a shape change takes a new version.
    """
    lines = golden.splitlines()
    rows = [line.split() for line in lines if line and not line.startswith("#")]
    pinned = [(tuple(int(part) for part in key.split(".")), digest) for key, digest in rows]
    versions = [key for key, _ in pinned]
    current = ".".join(str(part) for part in version)
    digest = schema_digest(schema)
    problem = None
    if versions != sorted(set(versions)):
        problem = f"the golden's versions are not ascending: {versions}"
    elif len({pinned_digest for _, pinned_digest in pinned}) != len(pinned):
        problem = "the golden: two versions pin the same shape"
    elif not pinned or pinned[-1][0] != version:
        problem = (
            f"the golden does not end at the current version {current}: "
            f"append the line '{current} {digest}'"
        )
    elif pinned[-1][1] != digest:
        problem = (
            f"the persisted shape changed under version {current}: bump the version and "
            f"append the line '<new version> {digest}' to the golden"
        )
    if problem is not None:
        raise AssertionError(problem)
