"""Tests for canonical orchestration roadmap and progress memory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration.memory import (
    declared_memory_paths,
    ensure_progress_file,
    ensure_roadmap_file,
    framework_memory_paths,
    pareto_archive_path,
    read_progress,
    read_roadmap,
    resolve_paths,
    structured_artifact_root,
    write_pareto_archive,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_resolve_paths_uses_only_canonical_directories(tmp_path: Path) -> None:
    legacy_roadmap = tmp_path / "roadmap.md"
    legacy_progress = tmp_path / "progress.md"
    legacy_roadmap.write_text("legacy roadmap\n")
    legacy_progress.write_text("legacy progress\n")

    roadmap, progress = resolve_paths(tmp_path)

    assert (roadmap, progress) == (tmp_path / "roadmap", tmp_path / "progress")
    assert legacy_roadmap.read_text() == "legacy roadmap\n"
    assert legacy_progress.read_text() == "legacy progress\n"


def test_framework_memory_paths_declare_only_canonical_roots(tmp_path: Path) -> None:
    assert framework_memory_paths(tmp_path) == (
        tmp_path / "roadmap",
        tmp_path / "progress",
    )
    assert declared_memory_paths() == ("roadmap", "progress")


def test_derived_artifacts_stay_under_progress(tmp_path: Path) -> None:
    progress = tmp_path / "progress"

    assert structured_artifact_root(progress) == progress
    assert pareto_archive_path(progress) == progress / "pareto-frontier.md"


def test_write_pareto_archive_creates_parents_and_formats_summary(tmp_path: Path) -> None:
    progress = tmp_path / "progress"

    path = write_pareto_archive(progress, "- candidate A: 1.2x\n\n")

    assert path == progress / "pareto-frontier.md"
    assert path.read_text() == "# Pareto frontier\n\n- candidate A: 1.2x\n"


def test_roadmap_seed_is_idempotent(tmp_path: Path) -> None:
    roadmap = tmp_path / "roadmap"
    ensure_roadmap_file(roadmap)
    document = roadmap / "index.md"
    seeded = document.read_text()
    assert "# Roadmap" in seeded

    document.write_text(seeded + "\nAppended by a round.\n")
    ensure_roadmap_file(roadmap)

    assert "Appended by a round." in read_roadmap(roadmap)


def test_read_roadmap_returns_empty_string_when_missing(tmp_path: Path) -> None:
    assert read_roadmap(tmp_path / "roadmap") == ""


def test_progress_seed_is_idempotent(tmp_path: Path) -> None:
    progress = tmp_path / "progress"
    ensure_progress_file(progress)
    readme = progress / "README.md"
    readme.write_text("custom notes\n")

    ensure_progress_file(progress)

    assert readme.read_text() == "custom notes\n"


def test_read_progress_returns_only_requested_recent_rounds(tmp_path: Path) -> None:
    progress = tmp_path / "progress"
    progress.mkdir()
    for number in range(1, 7):
        (progress / f"round-{number:04d}.md").write_text(f"round {number} notes")

    recent = read_progress(progress, recent_rounds=2)

    assert "round 5 notes" in recent
    assert "round 6 notes" in recent
    assert "round 4 notes" not in recent
    assert "round 1" in read_progress(progress, recent_rounds=0)


def test_read_progress_returns_empty_string_when_missing(tmp_path: Path) -> None:
    assert read_progress(tmp_path / "progress") == ""


def test_memory_helpers_do_not_probe_legacy_files(tmp_path: Path) -> None:
    legacy_roadmap = tmp_path / "roadmap.md"
    legacy_progress = tmp_path / "progress.md"
    legacy_roadmap.write_text("legacy roadmap\n")
    legacy_progress.write_text("legacy progress\n")

    ensure_roadmap_file(tmp_path / "roadmap")
    ensure_progress_file(tmp_path / "progress")

    assert read_roadmap(tmp_path / "roadmap") != legacy_roadmap.read_text()
    assert read_progress(tmp_path / "progress") == ""
    assert legacy_roadmap.read_text() == "legacy roadmap\n"
    assert legacy_progress.read_text() == "legacy progress\n"
