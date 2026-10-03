"""Tests for orchestration-owned memory path declarations."""

from __future__ import annotations

from vibesys.orchestration.memory import declared_memory_paths


def test_memory_paths_declare_only_canonical_roots() -> None:
    assert declared_memory_paths() == ("roadmap", "progress")
