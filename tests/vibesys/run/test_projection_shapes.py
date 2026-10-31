"""A projector change that alters what a scenario projects to shows in review as a text diff."""

from __future__ import annotations

import pytest
from tests.support.golden_regeneration import REGENERATE
from tests.support.projection_shapes import SNAPSHOTS, names, project, shape, snapshot_path


@pytest.mark.parametrize("name", names())
def test_a_scenario_projects_to_the_shape_its_snapshot_records(name: str) -> None:
    path = snapshot_path(name)
    assert path.is_file(), f"{path.name} is missing; write it with {REGENERATE}"

    actual = shape(name, project(name))

    assert actual == path.read_text(), (
        f"{name} no longer projects to the shape in {path.name}. If the change is intended, run "
        f"{REGENERATE} and commit the diff: it is what a reviewer reads in place of the replay"
    )


def test_no_snapshot_remains_for_a_scenario_that_is_gone() -> None:
    wanted = {snapshot_path(name).name for name in names()}

    assert {path.name for path in SNAPSHOTS.glob("*.txt")} == wanted, (
        f"a snapshot has no scenario, or a scenario has no snapshot; run {REGENERATE}"
    )
