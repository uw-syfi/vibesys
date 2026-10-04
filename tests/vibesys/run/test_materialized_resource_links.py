"""Resource citations and copied skill links share one confined layout."""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vibesys.constants import ComputeBackend
from vibesys.orchestration.skill_selection import (
    platform_skill_excluded_paths,
    platform_skill_selection,
    resolve_agent_resource_paths,
)
from vs_agent.api import materialize_skills


def _source(root: Path, target: Path) -> Path:
    skill = root / "source" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Demo")
    resource = skill / target
    resource.parent.mkdir(parents=True, exist_ok=True)
    resource.write_text("Reference.")
    return skill


@pytest.mark.parametrize("absolute", [False, True])
def test_reviewer_alias_to_pruned_platform_is_rejected_and_removed(
    tmp_path: Path, *, absolute: bool
) -> None:
    target = Path("references/platforms/cuda/floor.md")
    skill = _source(tmp_path, target)
    (skill / "alias.md").symlink_to(skill / target if absolute else target)
    workspace = tmp_path / "workspace"
    materialize_skills(workspace, [skill], selection=platform_skill_selection(ComputeBackend.ROCM))
    citation = "resources/skills/demo/alias.md"
    with pytest.raises(ValueError, match="excluded from this run"):
        resolve_agent_resource_paths(
            citation,
            [skill],
            excluded_relative_paths=platform_skill_excluded_paths(ComputeBackend.ROCM),
        )
    assert not (workspace / ".agents/skills/demo/alias.md").is_symlink()


def test_reviewer_absolute_internal_link_resolves_in_actual_copied_tree(tmp_path: Path) -> None:
    target = Path("references/rocm-note.md")
    skill = _source(tmp_path, target)
    (skill / "absolute.md").symlink_to(skill / target)
    workspace = tmp_path / "workspace"
    materialize_skills(workspace, [skill])
    citation = "resources/skills/demo/absolute.md"
    resolved = resolve_agent_resource_paths(citation, [skill])
    assert resolved == ".agents/skills/demo/references/rocm-note.md"
    assert (workspace / resolved).resolve().is_relative_to(workspace)
    copied_link = workspace / ".agents/skills/demo/absolute.md"
    assert copied_link.resolve().is_relative_to(copied_link.parent)
    assert copied_link.read_text() == "Reference."


@given(
    target_parts=st.lists(
        st.from_regex(r"[a-z][a-z0-9_-]{0,8}", fullmatch=True), min_size=1, max_size=4
    ),
    absolute_links=st.lists(st.booleans(), min_size=1, max_size=5),
    excluded=st.booleans(),
)
@example(target_parts=["references", "notes"], absolute_links=[True, False], excluded=False)
def test_link_chains_only_advertise_resources_inside_materialized_skill(
    *, target_parts: list[str], absolute_links: list[bool], excluded: bool
) -> None:
    with TemporaryDirectory() as scratch:
        root = Path(scratch)
        prefix = Path("references/platforms/cuda") if excluded else Path("references")
        target = prefix / Path(*target_parts).with_suffix(".md")
        skill = _source(root, target)
        previous = target
        for index, absolute in enumerate(absolute_links):
            alias = Path(f"alias-{index}.md")
            (skill / alias).symlink_to(skill / previous if absolute else previous)
            previous = alias
        workspace = root / "workspace"
        materialize_skills(
            workspace, [skill], selection=platform_skill_selection(ComputeBackend.ROCM)
        )
        citation = f"resources/skills/demo/{previous}"
        if excluded:
            with pytest.raises(ValueError, match="excluded from this run"):
                resolve_agent_resource_paths(
                    citation,
                    [skill],
                    excluded_relative_paths=platform_skill_excluded_paths(ComputeBackend.ROCM),
                )
        else:
            resolved = resolve_agent_resource_paths(citation, [skill])
            installed = workspace / resolved
            assert installed.read_text() == "Reference."
            assert installed.resolve().is_relative_to(workspace / ".agents/skills/demo")
        for copied_skill in (workspace / "demo", workspace / ".agents/skills/demo"):
            for link in copied_skill.rglob("*"):
                if link.is_symlink():
                    assert link.exists()
                    assert link.resolve().is_relative_to(copied_skill)


@pytest.mark.parametrize("target", ["repos/secret.md", ".git/config", "__pycache__/cache.pyc"])
def test_generic_exclusions_apply_to_resolved_alias_targets(tmp_path: Path, target: str) -> None:
    skill = _source(tmp_path, Path(target))
    (skill / "alias.md").symlink_to(target)
    materialize_skills(tmp_path / "workspace", [skill])
    with pytest.raises(ValueError, match="outside agent-visible"):
        resolve_agent_resource_paths("resources/skills/demo/alias.md", [skill])
    assert not (tmp_path / "workspace/.agents/skills/demo/alias.md").is_symlink()


@pytest.mark.parametrize("kind", ["outside", "dangling", "cycle"])
def test_materialization_removes_invalid_links(tmp_path: Path, kind: str) -> None:
    skill = _source(tmp_path, Path("reference.md"))
    outside = tmp_path / "outside.md"
    outside.write_text("Outside.")
    link = skill / "alias.md"
    target = {"outside": outside, "dangling": Path("missing.md"), "cycle": Path("alias.md")}[kind]
    link.symlink_to(target)
    materialize_skills(tmp_path / "workspace", [skill])
    assert not (tmp_path / "workspace/.agents/skills/demo/alias.md").is_symlink()


@given(absolute=st.booleans(), excluded=st.booleans())
def test_directory_aliases_resolve_only_to_copied_resources(
    *, absolute: bool, excluded: bool
) -> None:
    with TemporaryDirectory() as scratch:
        root = Path(scratch)
        directory = Path("references/platforms/cuda") if excluded else Path("references/notes")
        skill = _source(root, directory / "reference.md")
        (skill / "alias").symlink_to(
            skill / directory if absolute else directory, target_is_directory=True
        )
        workspace = root / "workspace"
        materialize_skills(
            workspace, [skill], selection=platform_skill_selection(ComputeBackend.ROCM)
        )
        citation = "resources/skills/demo/alias/reference.md"
        if excluded:
            with pytest.raises(ValueError, match="excluded from this run"):
                resolve_agent_resource_paths(
                    citation,
                    [skill],
                    excluded_relative_paths=platform_skill_excluded_paths(ComputeBackend.ROCM),
                )
            assert not (workspace / ".agents/skills/demo/alias").is_symlink()
        else:
            resolved = resolve_agent_resource_paths(citation, [skill])
            assert (workspace / resolved).read_text() == "Reference."
            assert (
                (workspace / ".agents/skills/demo/alias/reference.md")
                .resolve()
                .is_relative_to(workspace)
            )
