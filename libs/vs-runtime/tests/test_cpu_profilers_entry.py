"""The standard-library-only profiler surface the in-container linux-cpu server imports.

The server file runs on the container's interpreter and cannot be imported here, so these
tests drive the same names it imports, from ``vs_runtime.cpu_profilers``.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Never

from hypothesis import given
from hypothesis import strategies as st

# test-isolation: vs_runtime.cpu_profilers is the declared public entry (tach expose) for the in-container server, whose own api is out of reach there.
from vs_runtime import cpu_profilers
from vs_runtime.api import infrastructure

# test-isolation: vs_runtime.cpu_profilers is the declared public entry (tach expose) for the in-container server, whose own api is out of reach there.
from vs_runtime.cpu_profilers import (
    detect_linux_profiler,
    parse_profile_command,
    summarize_linux_profile,
)
from vs_sim.api.testing import FakeProcesses, ManualClock

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sim.api import ProcessSpec


def test_the_container_surface_is_the_same_mechanism_the_host_api_exposes() -> None:
    assert sorted(cpu_profilers.__all__) == [
        "LinuxProfilerEffects",
        "collect_linux_profile",
        "detect_linux_profiler",
        "parse_profile_command",
        "summarize_linux_profile",
    ]
    for name in cpu_profilers.__all__:
        assert getattr(cpu_profilers, name) is getattr(infrastructure, name)


@given(
    st.lists(
        st.text(st.characters(codec="ascii", categories=("L", "N", "P", "Zs")), min_size=1),
        min_size=1,
        max_size=5,
    )
)
def test_a_quoted_argv_parses_back_to_itself(argv: list[str]) -> None:
    assert parse_profile_command(shlex.join(argv)) == argv


def test_an_empty_output_directory_summarizes_without_metadata(tmp_path: Path) -> None:
    assert summarize_linux_profile(tmp_path)["metadata"] is None


def _never_runs(spec: ProcessSpec) -> Never:
    raise AssertionError(spec.argv)


def test_capability_detection_names_a_tool() -> None:
    processes = FakeProcesses(_never_runs, pass_time=ManualClock().advance)
    assert detect_linux_profiler(system="Darwin", processes=processes).tool.value
