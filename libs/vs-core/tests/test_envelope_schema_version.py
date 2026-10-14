"""The envelope's shape is pinned to its version, so a shape change cannot keep an old number.

A run is resumed from its stored ``RunEnvelope``; an envelope whose version matches but
whose shape differs fails with a generic validation error instead of the version check.
``envelope_schema_versions.txt`` lists, one line per version, the version and a hash of
the envelope's JSON schema. Changing the shape without adding a line for a new version
fails here, and two branches that each take the next version both append to the same
last line, so they conflict in the file instead of merging silently.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from vs_core.api import ENVELOPE_SCHEMA_VERSION, RunEnvelope, StrategyState

_VERSIONS = Path(__file__).parent / "envelope_schema_versions.txt"


def _schema_hash() -> str:
    schema = RunEnvelope[StrategyState].model_json_schema()
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _pinned() -> list[tuple[int, str]]:
    lines = _VERSIONS.read_text(encoding="utf-8").splitlines()
    rows = [line.split() for line in lines if line and not line.startswith("#")]
    return [(int(version), digest) for version, digest in rows]


def test_the_envelope_shape_is_the_one_pinned_for_its_version() -> None:
    version, digest = _pinned()[-1]
    assert version == ENVELOPE_SCHEMA_VERSION, (
        f"{_VERSIONS.name} ends at version {version}, but ENVELOPE_SCHEMA_VERSION is "
        f"{ENVELOPE_SCHEMA_VERSION}: append the line '{ENVELOPE_SCHEMA_VERSION} {_schema_hash()}'"
    )
    assert digest == _schema_hash(), (
        f"the envelope's shape changed under version {version}: bump ENVELOPE_SCHEMA_VERSION "
        f"and append the line '<new version> {_schema_hash()}' to {_VERSIONS.name}"
    )


def test_each_pinned_version_is_newer_and_names_a_different_shape() -> None:
    pinned = _pinned()
    versions = [version for version, _ in pinned]
    digests = [digest for _, digest in pinned]
    assert versions == sorted(set(versions)), versions
    assert len(set(digests)) == len(digests), "two versions pin the same shape"
