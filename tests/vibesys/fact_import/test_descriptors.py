"""Frozen snapshot descriptors cannot drift from the envelope version that wrote them."""

from __future__ import annotations

from hashlib import sha256

from tests.vibesys.fact_import.descriptors import (
    DIRECTORY,
    current_path,
    live_descriptors,
    render,
)

# Every frozen file except the current envelope version's. When ENVELOPE_SCHEMA_VERSION is
# bumped, the previous version's file joins this table and is never edited again.
_PINNED = {
    "envelope-v42.json": "6a3216d9cd8adbcbf0ce876a10836575577e1b8294c43d861d6b532edde83894",
    "retained.json": "1d651d6d14af8db053ad400ce002812c26c9f1a17ede3ee47a14716503a2e7da",
}


def test_the_current_envelope_version_has_frozen_descriptors_equal_to_the_live_models() -> None:
    path = current_path()
    assert path.is_file(), (
        f"no frozen descriptors for {path.name}; run "
        "`uv run --locked python -m tests.vibesys.fact_import.descriptors`"
    )
    assert path.read_text() == render(live_descriptors()), (
        f"the live models changed {path.name}: if every saved envelope still validates "
        "(scripts/check_persisted_schema_compat.py), regenerate it with "
        "`uv run --locked python -m tests.vibesys.fact_import.descriptors`; otherwise bump "
        "ENVELOPE_SCHEMA_VERSION and freeze the new version instead"
    )


def test_every_older_frozen_descriptor_file_is_pinned_by_hash() -> None:
    frozen = {
        path.name: sha256(path.read_bytes()).hexdigest()
        for path in DIRECTORY.glob("*.json")
        if path != current_path()
    }
    assert frozen == _PINNED
