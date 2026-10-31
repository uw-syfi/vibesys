"""A read and the host helper build the facts they share the same way.

``ReadHost`` (a read's facts, built one at a time) and ``real_host`` (the helper's, built all at
start) wire the registry, projects, directories, examples, logins, compute settings and harnesses
separately: the helper imports its collaborators statically before it answers ``hello``, which a
shared lazy wiring would add modules to (``tests/e2e/test_host_helper.py`` counts them). These
tests hold the two wirings to each other, so a collaborator changed in one and not the other
fails here and not in a field report. The facts are found from the two public surfaces, so a fact
added to both is covered without editing the tests.
"""

from __future__ import annotations

import dataclasses
import itertools
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.task_project import write_task

from entrypoints.host_facts import HostFacts
from entrypoints.host_helper import real_host
from entrypoints.host_reads import ReadHost
from server.host_helper_requests import HelperHost
from vs_project.api import Project
from vs_sim.api.testing import FakeProcesses, ProcessScript, SimThreads

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sim.api.testing import Sim

_INJECTED = frozenset({"clock"})
"""The epoch clock is the one shared fact each side is handed: the helper's is the system clock and
a read's is its platform's (the same clock in production)."""
_ON_USE = frozenset({"examples"})
"""The helper loads the example machinery on its first request and a read builds it when asked, so
the two are different objects that must answer alike."""

SHARED = sorted(
    {field.name for field in dataclasses.fields(HelperHost)}
    & {name for name in dir(HostFacts) if not name.startswith("_")}
)
"""Every fact the helper's host and a read's host both have, found from the two public surfaces,
so a fact added to both is covered without editing this test."""


@pytest.fixture
def environment(tmp_path: Path) -> dict[str, str]:
    """A private runtime directory and configuration root: nothing of this machine's."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    return {
        "XDG_RUNTIME_DIR": str(runtime),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "HOME": str(tmp_path),
    }


def _read_host(environment: dict[str, str], sim: Sim) -> ReadHost:
    threads = sim.threads()
    return ReadHost(
        environment,
        os.getuid(),
        clock=sim.wall_clock,
        threads=threads,
        processes=FakeProcesses(lambda _: ProcessScript(), pass_time=threads.sleep),
    )


def test_the_facts_found_to_be_shared_include_the_ones_wired_twice_before() -> None:
    """Finding the shared facts from the two surfaces must not come up empty or short."""
    assert {"registry", "projects", "directories", "examples", "logins", "compute"} <= set(SHARED)


@pytest.mark.parametrize("name", [name for name in SHARED if name not in _INJECTED | _ON_USE])
def test_a_shared_fact_is_the_same_kind_of_object_for_a_read_and_the_helper(
    name: str, environment: dict[str, str], sim: Sim
) -> None:
    helper = real_host(environment, os.getuid(), SimThreads())
    read = _read_host(environment, sim)

    if name == "harnesses":
        assert helper.harnesses() == read.harnesses()
    else:
        assert type(getattr(helper, name)) is type(getattr(read, name))


def test_the_examples_a_read_lists_are_the_ones_the_helper_lists(
    environment: dict[str, str], sim: Sim
) -> None:
    helper = real_host(environment, os.getuid(), SimThreads())
    read = _read_host(environment, sim)

    assert read.examples.list() == helper.examples.list()


_TASK_NAMES = st.lists(
    st.from_regex(r"[a-z][a-z0-9]{0,7}", fullmatch=True), unique=True, min_size=1, max_size=4
)
_COUNTER = itertools.count()


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=10)
@given(names=_TASK_NAMES)
def test_a_read_and_the_helper_answer_a_project_alike(
    names: list[str], environment: dict[str, str], sim: Sim, tmp_path: Path
) -> None:
    """The same project, listed through the helper's facts and a read's, reads the same."""
    project = tmp_path / f"project-{next(_COUNTER)}"
    project.mkdir()
    Project.open(project).state.create_project("example", now=datetime(2026, 1, 1, tzinfo=UTC))
    for name in names:
        write_task(project, "", task=name)
    helper = real_host(environment, os.getuid(), SimThreads())
    read = _read_host(environment, sim)

    assert read.projects.tasks(project) == helper.projects.tasks(project)
    for name in names:
        assert read.projects.task_goal(project, name) == helper.projects.task_goal(project, name)
    assert read.directories.home() == helper.directories.home()
    assert read.directories.expand("~/a") == helper.directories.expand("~/a")
    assert read.registry.read() == helper.registry.read()
    assert read.compute.settings() == helper.compute.settings()
