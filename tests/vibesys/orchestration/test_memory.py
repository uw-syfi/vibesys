"""Tests for orchestration-owned memory path declarations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration.memory import declared_memory_paths, framework_memory_paths

if TYPE_CHECKING:
    from pathlib import Path


def test_memory_paths_declare_only_canonical_roots(tmp_path: Path) -> None:
    assert framework_memory_paths(tmp_path) == (
        tmp_path / "roadmap",
        tmp_path / "progress",
    )
    assert declared_memory_paths() == ("roadmap", "progress")
