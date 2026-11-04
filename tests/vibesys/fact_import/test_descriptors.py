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
    "envelope-v42.json": "2a98bcf319b3ba27df5a24ebaab8fea0613cc16b6665d7b0bfab3f682fd258a0",
    "envelope-v43.json": "250c0f42dd62fa9bf8c5ab96edce8b38aa3a91998307e3da6bc5ec1dde431665",
    "envelope-v44.json": "9a94b93414f90306a7f9b7d998f6b939c23b64beca3d5fb0f5da90126e930844",
    "envelope-v45.json": "49b7a67debabead60f77bcaaa57338e5614a9f5f9e7457cabc46ec89d7a93e90",
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
