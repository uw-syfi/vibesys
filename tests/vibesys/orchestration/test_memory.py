"""``vibesys.orchestration.memory``: roadmap, progress, and layout resolution.

``test_declared_memory.py`` covers the host's automatic preservation of
declared memory paths through workspace rollback; it never calls into this
module's own functions. These tests exercise ``vibesys.orchestration.memory``
directly: layout resolution (legacy file vs. directory, and the conflict
between them), the derived framework-owned paths (structured artifact root,
Pareto archive), and the roadmap/progress read-and-seed helpers, for both
supported layouts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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

# ---------------------------------------------------------------------------
# resolve_paths: layout selection and conflict detection
# ---------------------------------------------------------------------------


def test_resolve_paths_rejects_unknown_layout(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown memory layout"):
        resolve_paths(tmp_path, "nonexistent")


def test_resolve_paths_prefers_legacy_file_when_only_it_exists(tmp_path: Path) -> None:
    (tmp_path / "roadmap.md").write_text("existing roadmap\n")

    roadmap, _progress = resolve_paths(tmp_path, "directories")

    assert roadmap == tmp_path / "roadmap.md"


def test_resolve_paths_prefers_existing_directory_when_only_it_exists(tmp_path: Path) -> None:
    (tmp_path / "progress").mkdir()

    _roadmap, progress = resolve_paths(tmp_path, "files")

    assert progress == tmp_path / "progress"


def test_resolve_paths_falls_back_to_requested_layout_when_neither_exists(tmp_path: Path) -> None:
    roadmap, progress = resolve_paths(tmp_path, "directories")
    assert (roadmap, progress) == (tmp_path / "roadmap", tmp_path / "progress")

    roadmap, progress = resolve_paths(tmp_path, "files")
    assert (roadmap, progress) == (tmp_path / "roadmap.md", tmp_path / "progress.md")


def test_resolve_paths_rejects_both_layouts_present(tmp_path: Path) -> None:
    (tmp_path / "roadmap.md").write_text("legacy\n")
    (tmp_path / "roadmap").mkdir()

    with pytest.raises(ValueError, match="keep only one"):
        resolve_paths(tmp_path, "files")


# ---------------------------------------------------------------------------
# Derived framework-owned paths
# ---------------------------------------------------------------------------


def test_structured_artifact_root_legacy_file_uses_sibling_directory(tmp_path: Path) -> None:
    progress_path = tmp_path / "progress.md"

    assert structured_artifact_root(progress_path) == tmp_path / "progress-artifacts"


def test_structured_artifact_root_directory_layout_is_itself(tmp_path: Path) -> None:
    progress_path = tmp_path / "progress"

    assert structured_artifact_root(progress_path) == progress_path


def test_pareto_archive_path_legacy_vs_directory(tmp_path: Path) -> None:
    assert pareto_archive_path(tmp_path / "progress.md") == tmp_path / "pareto-frontier.md"
    assert (
        pareto_archive_path(tmp_path / "progress") == tmp_path / "progress" / "pareto-frontier.md"
    )


def test_framework_memory_paths_covers_both_layouts_and_has_no_duplicates(tmp_path: Path) -> None:
    paths = framework_memory_paths(tmp_path)

    assert len(paths) == len(set(paths))
    assert tmp_path / "roadmap.md" in paths
    assert tmp_path / "roadmap" in paths
    assert tmp_path / "progress.md" in paths
    assert tmp_path / "progress" in paths
    # Derived artifact/Pareto roots for both progress shapes are included.
    assert tmp_path / "progress-artifacts" in paths
    assert tmp_path / "pareto-frontier.md" in paths
    assert tmp_path / "progress" / "pareto-frontier.md" in paths


def test_declared_memory_paths_are_workspace_relative_strings() -> None:
    declared = declared_memory_paths()

    assert declared == tuple(str(p) for p in framework_memory_paths(Path()))
    assert all(not p.startswith("/") for p in declared)


# ---------------------------------------------------------------------------
# Pareto archive
# ---------------------------------------------------------------------------


def test_write_pareto_archive_creates_parents_and_formats_summary(tmp_path: Path) -> None:
    progress_path = tmp_path / "progress" / "round-0001.md"

    path = write_pareto_archive(progress_path, "- candidate A: 1.2x\n\n")

    assert path == pareto_archive_path(progress_path)
    assert path.read_text() == "# Pareto frontier\n\n- candidate A: 1.2x\n"


# ---------------------------------------------------------------------------
# Roadmap: legacy file and directory layouts
# ---------------------------------------------------------------------------


def test_ensure_roadmap_file_seeds_legacy_file_once(tmp_path: Path) -> None:
    roadmap_path = tmp_path / "roadmap.md"

    ensure_roadmap_file(roadmap_path)
    seeded = roadmap_path.read_text()
    assert "# Roadmap" in seeded

    roadmap_path.write_text(seeded + "\nAppended by a round.\n")
    ensure_roadmap_file(roadmap_path)  # idempotent: does not clobber existing content

    assert "Appended by a round." in roadmap_path.read_text()


def test_ensure_roadmap_file_seeds_directory_layout_index(tmp_path: Path) -> None:
    roadmap_path = tmp_path / "roadmap"

    ensure_roadmap_file(roadmap_path)

    assert (roadmap_path / "index.md").exists()
    assert "# Roadmap" in (roadmap_path / "index.md").read_text()


def test_read_roadmap_returns_empty_string_when_missing(tmp_path: Path) -> None:
    assert read_roadmap(tmp_path / "roadmap.md") == ""
    assert read_roadmap(tmp_path / "roadmap") == ""


def test_read_roadmap_reads_directory_layout_index(tmp_path: Path) -> None:
    roadmap_path = tmp_path / "roadmap"
    ensure_roadmap_file(roadmap_path)

    assert read_roadmap(roadmap_path) == (roadmap_path / "index.md").read_text()


# ---------------------------------------------------------------------------
# Progress: legacy file and directory layouts
# ---------------------------------------------------------------------------


def test_ensure_progress_file_seeds_legacy_file_with_header(tmp_path: Path) -> None:
    progress_path = tmp_path / "progress.md"

    ensure_progress_file(progress_path)

    assert progress_path.read_text() == "# Progress\n\n"


def test_ensure_progress_file_seeds_directory_with_readme(tmp_path: Path) -> None:
    progress_path = tmp_path / "progress"

    ensure_progress_file(progress_path)

    assert progress_path.is_dir()
    assert (progress_path / "README.md").exists()


def test_ensure_progress_file_does_not_clobber_existing_readme(tmp_path: Path) -> None:
    progress_path = tmp_path / "progress"
    ensure_progress_file(progress_path)
    (progress_path / "README.md").write_text("custom notes\n")

    ensure_progress_file(progress_path)

    assert (progress_path / "README.md").read_text() == "custom notes\n"


def test_read_progress_missing_path_returns_empty_string(tmp_path: Path) -> None:
    assert read_progress(tmp_path / "progress.md") == ""


def test_read_progress_reads_legacy_file_whole(tmp_path: Path) -> None:
    progress_path = tmp_path / "progress.md"
    progress_path.write_text("# Progress\n\nround 1\n")

    assert read_progress(progress_path) == "# Progress\n\nround 1\n"


def test_read_progress_directory_layout_bounds_to_recent_rounds(tmp_path: Path) -> None:
    progress_path = tmp_path / "progress"
    progress_path.mkdir()
    for n in range(1, 7):
        (progress_path / f"round-{n:04d}.md").write_text(f"round {n} notes")

    recent = read_progress(progress_path, recent_rounds=2)

    assert "round 5 notes" in recent
    assert "round 6 notes" in recent
    assert "round 4 notes" not in recent


def test_read_progress_directory_layout_unbounded_when_recent_rounds_zero(tmp_path: Path) -> None:
    progress_path = tmp_path / "progress"
    progress_path.mkdir()
    for n in range(1, 4):
        (progress_path / f"round-{n:04d}.md").write_text(f"round {n}")

    all_rounds = read_progress(progress_path, recent_rounds=0)

    assert "round 1" in all_rounds
    assert "round 3" in all_rounds
