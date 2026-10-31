"""The key of a derived replay set names every file whose code runs while a replay is projected.

A set is stored under ``derived_replays.key``, so a file that changes the projection but is missing
from ``INPUTS`` would leave a stale set in use. This test runs the projection of a sample of
declarations and fails, naming each file whose code ran outside the key.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from tests.support import journey_fixtures, shared_scenarios
from tests.support.derived_replays import REPOSITORY, covered, input_files, key

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import CodeType

#: Small declarations that reach different mappings: a quota pause, a failed input gate, retries
#: and a turn with tool calls. The 600 idea scenarios and the live prefixes run the same code for
#: minutes.
_SHARED = ("quota-pause", "input-gate-failed", "retries", "agent-turn-rich")
_HEAVY_JOURNEYS = frozenset({"lineage600"})


def _files_that_run(action: Callable[[], None]) -> set[Path]:
    """The repository files with a function that started while ``action`` ran."""
    monitoring = sys.monitoring
    tool = monitoring.PROFILER_ID
    files: set[str] = set()

    def started(code: CodeType, _offset: int) -> object:
        files.add(code.co_filename)
        return monitoring.DISABLE

    monitoring.use_tool_id(tool, "derived-replay-inputs")
    try:
        monitoring.register_callback(tool, monitoring.events.PY_START, started)
        monitoring.set_events(tool, monitoring.events.PY_START)
        action()
    finally:
        monitoring.set_events(tool, 0)
        monitoring.register_callback(tool, monitoring.events.PY_START, None)
        monitoring.free_tool_id(tool)
    return {Path(name).resolve() for name in files if Path(name).is_absolute()}


def _project_a_sample() -> None:
    for name in _SHARED:
        shared_scenarios.project_shared(shared_scenarios.SOURCES / f"{name}.json")
    for path in sorted(journey_fixtures.ROOT.glob("*.json")):
        if path.stem not in _HEAVY_JOURNEYS:
            journey_fixtures.project_scenario(path.stem)
            journey_fixtures.scenario_variants(path.stem)


def test_derived_replay_inputs_cover_every_file_that_runs_while_projecting() -> None:
    ran = _files_that_run(_project_a_sample)
    repository = {
        path
        for path in ran
        if REPOSITORY in path.parents
        and ".venv" not in path.relative_to(REPOSITORY).parts
        and path != Path(__file__).resolve()
    }

    outside = sorted(path.relative_to(REPOSITORY) for path in repository if not covered(path))

    assert repository, (
        "tracing saw no repository file run; the tracer is not observing the projection"
    )
    assert not outside, (
        "projection ran code that tests/support/derived_replays.py INPUTS does not name, so a "
        "change to it would leave a stale replay set in use. Add each to INPUTS:\n"
        + "\n".join(f"  {path}" for path in outside)
    )


def test_every_derived_replay_input_exists_and_is_read_by_the_key() -> None:
    assert input_files(), "INPUTS names no file"
    assert len(key()) == 32
