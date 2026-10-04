"""Source resource citations resolve to installed, confined skill inputs."""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vibesys.orchestration.skill_selection import resolve_agent_resource_paths


@given(st.lists(st.from_regex(r"[a-z][a-z0-9_-]{0,12}", fullmatch=True), min_size=1, max_size=4))
def test_source_skill_paths_resolve_without_changing_objective_text(parts: list[str]) -> None:
    with TemporaryDirectory() as scratch:
        skill = Path(scratch) / "serving-systems"
        resource = Path(*parts).with_suffix(".md")
        target = skill / resource
        target.parent.mkdir(parents=True)
        target.write_text("Reference.")
        citation = f"resources/skills/serving-systems/{resource.as_posix()}"
        objective = f"Read `{citation}`. Constraint: never start a full-model server."
        resolved = resolve_agent_resource_paths(objective, [skill])
        expected = f".agents/skills/serving-systems/{resource.as_posix()}"
        assert resolved == objective.replace(citation, expected)
        assert resolve_agent_resource_paths(resolved, [skill]) == resolved


@pytest.mark.parametrize(
    ("resource", "diagnostic"),
    [
        ("missing.md", "missing or escaping"),
        ("../outside.md", "outside agent-visible"),
        ("repos/engine/source.py", "outside agent-visible"),
        (".git/config", "outside agent-visible"),
        ("__pycache__/module.pyc", "outside agent-visible"),
        ("escape.md", "missing or escaping"),
        ("references/platforms/cuda/floor.md", "excluded from this run"),
    ],
)
def test_unavailable_source_resources_fail_before_an_agent_turn(
    tmp_path: Path, resource: str, diagnostic: str
) -> None:
    skill = tmp_path / "serving-systems"
    skill.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("Outside confinement.")
    (skill / "escape.md").symlink_to(outside)
    excluded = Path("references/platforms/cuda")
    (skill / excluded).mkdir(parents=True)
    (skill / excluded / "floor.md").write_text("Wrong backend.")
    citation = f"resources/skills/serving-systems/{resource}"
    with pytest.raises(ValueError, match=diagnostic) as error:
        resolve_agent_resource_paths(
            citation, [skill], excluded_relative_paths=frozenset({excluded})
        )
    assert citation in str(error.value)


def test_uninstalled_skill_is_not_silently_rewritten() -> None:
    citation = "resources/skills/unknown/SKILL.md"
    with pytest.raises(ValueError, match="skill is not installed") as error:
        resolve_agent_resource_paths(citation, [])
    assert citation in str(error.value)
