"""Every built-in search ends cleanly whatever core does to its waits and however it is stopped.

Each built-in orchestration registers how its host starts and the configs it runs (`SEARCHES`),
and the shared fault menu of `tests.vibesys.search_faults` does the rest: a generated story
answers waits the search's own way or with any fault core can deliver, the operator may stop
(draining or cancelling) at any point, and the run must still end in one `Finish` with every
attempt settled. After every step, what the run published must project, through the
registration's own projector, to the view the server and clients read. A new built-in fails
`test_every_built_in_search_is_played_under_faults` until it registers here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, note, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._host import begin as dynamic_begin
from tests.vibesys.orchestration.dynamic._host import config as dynamic_config
from tests.vibesys.orchestration.evolve._host import begin as evolve_begin
from tests.vibesys.orchestration.evolve._host import config as evolve_config
from tests.vibesys.orchestration.issue_queue._host import begin as queue_begin
from tests.vibesys.orchestration.issue_queue._host import config as queue_config
from tests.vibesys.orchestration.issue_queue._host import suspending_view
from tests.vibesys.orchestration.multi._host import MultiHost
from tests.vibesys.orchestration.multi._host import config as multi_config
from tests.vibesys.orchestration.single._host import SingleHost
from tests.vibesys.orchestration.single._host import config as single_config
from tests.vibesys.search_faults import FAULTS, MAX_STEPS, Step, play, steps, stops
from tests.vibesys.serial_search._host import SerialHost
from tests.vibesys.serial_search._replies import attribution
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.api.catalog import BUILT_INS
from vibesys.orchestration.input_check.search import InputCheckConfig, check_input
from vibesys.orchestration.single.search.api import single
from vibesys.plugin_registration import RuntimeRecordProjector
from vibesys.run.contracts import IterationTree
from vs_core.api import ResumeRefusal
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.vibesys.search_faults import SearchHost, Stop

PROFILE_GUIDED = {"command": ("bash", "attribute.sh"), "timeout_seconds": 10}
_FEW = st.integers(1, 3)
_SERIAL = {
    "max_rounds": _FEW,
    "max_retries_per_round": st.integers(1, 2),
    "max_continuation_rounds": st.integers(0, 2),
    "exclusive_root": st.booleans(),
}
_MULTI = {**_SERIAL, "judge_every": st.integers(1, 2)}
_GUIDED = {"attribution": st.just(PROFILE_GUIDED)}


@dataclass(frozen=True)
class Search:
    """How a built-in orchestration's host starts, under a config drawn from ``shapes``."""

    start: Callable[[dict[str, Any], bool], SearchHost]
    shapes: dict[str, st.SearchStrategy[Any]]


SEARCHES: dict[str, Search] = {
    "check-input": Search(
        lambda _shape, restarting: SerialHost.begin(
            check_input, InputCheckConfig(), restarting=restarting
        ),
        {},
    ),
    "dynamic": Search(
        lambda shape, restarting: dynamic_begin(dynamic_config(**shape), restarting=restarting),
        {
            "max_rounds": _FEW,
            "max_in_flight": _FEW,
            "judge_every": st.integers(1, 2),
            "max_unmeasured_turns": st.integers(1, 2),
            "max_continuation_rounds": st.integers(0, 2),
            "profiling": st.booleans(),
        },
    ),
    "evolve": Search(
        lambda shape, restarting: evolve_begin(evolve_config(**shape), restarting=restarting),
        {
            "max_generations": st.integers(1, 2),
            "children_per_generation": _FEW,
            "max_parallelism": _FEW,
            "k_top_inspirations": st.integers(0, 2),
            "k_random_inspirations": st.integers(0, 1),
            "bootstrap_max_attempts": _FEW,
            "search_policy": st.sampled_from(["vibesys", "openevolve"]),
            "profiling": st.booleans(),
        },
    ),
    "plain": Search(
        lambda shape, restarting: queue_begin(
            queue_config(**shape), restarting=restarting, view=suspending_view(), suspending=True
        ),
        {
            "max_rounds": _FEW,
            "max_attempts_per_issue": _FEW,
            "max_issues_per_perf_eval": _FEW,
            "max_issues_per_round": st.none() | _FEW,
            "max_board_retries": st.integers(0, 3),
        },
    ),
    "single-agent": Search(
        lambda shape, restarting: SingleHost.start(single_config(**shape), restarting=restarting),
        _SERIAL,
    ),
    "profile-guided-single-agent": Search(
        lambda shape, restarting: SingleHost.start(
            single_config(**shape), restarting=restarting, attribution_output=attribution()
        ),
        {**_SERIAL, **_GUIDED},
    ),
    "multi-agent": Search(
        lambda shape, restarting: MultiHost.start(
            multi_config(**shape), restarting=restarting, validating=True, suspending=True
        ),
        _MULTI,
    ),
    "profile-guided-multi-agent": Search(
        lambda shape, restarting: MultiHost.start(
            multi_config(**shape), restarting=restarting, attribution_output=attribution()
        ),
        {**_MULTI, **_GUIDED},
    ),
}
"""How each built-in orchestration's host starts and the configs it runs, by plugin id."""


_REGISTRATIONS = {item.plugin.id: item for item in BUILT_INS}


def test_every_built_in_search_is_played_under_faults() -> None:
    assert sorted(SEARCHES) == sorted(item.plugin.id for item in BUILT_INS)


@pytest.mark.parametrize("plugin_id", sorted(SEARCHES))
@across_restarts
@settings(max_examples=examples(pr=4, nightly=80), deadline=None, derandomize=True, database=None)
@given(data=st.data(), story=steps(), stop=stops())
def test_a_search_ends_cleanly_whatever_core_does_to_its_waits_and_however_it_stops(
    *, plugin_id: str, restarting: bool, data: st.DataObject, story: list[Step], stop: Stop | None
) -> None:
    search = SEARCHES[plugin_id]
    host = search.start(data.draw(st.fixed_dictionaries(search.shapes), label="config"), restarting)
    host.run.adopting = False
    projector = _REGISTRATIONS[plugin_id].projector
    assert isinstance(projector, RuntimeRecordProjector)

    def projects() -> None:
        """What the run published projects to the view the server and clients read, tree too."""
        published = projector.published(host.run.state)
        if published is None:
            return
        projection = projector.project(published, len(host.run.decisions))
        projection.model_dump_json()
        assert isinstance(projection.iterations, IterationTree), host.run.timeline()

    try:
        play(host, story, stop, check=projects)
    finally:
        note(host.run.timeline())


_FRESH = False
"""Started afresh, not restarted from a saved record."""

SUSPENDING: dict[str, Callable[[dict[str, Any]], SearchHost]] = {
    "dynamic": lambda shape: dynamic_begin(
        dynamic_config(**shape), restarting=False, view=suspending_view()
    ),
    "evolve": lambda shape: evolve_begin(
        evolve_config(**shape), restarting=False, view=suspending_view()
    ),
    "plain": lambda shape: SEARCHES["plain"].start(shape, _FRESH),
    "single-agent": lambda shape: SingleHost.begin(
        single, single_config(**shape), restarting=False, view=suspending_view()
    ),
    "multi-agent": lambda shape: SEARCHES["multi-agent"].start(shape, _FRESH),
}
"""How each built-in search that lets an agent wait for its evaluations starts, offering that."""


@pytest.mark.parametrize("reason", list(ResumeRefusal))
@pytest.mark.parametrize("plugin_id", sorted(SUSPENDING))
@settings(max_examples=examples(pr=2, nightly=20), deadline=None, derandomize=True, database=None)
@given(data=st.data())
def test_a_search_ends_cleanly_when_core_refuses_every_resume_it_waits_for(
    *, plugin_id: str, reason: ResumeRefusal, data: st.DataObject
) -> None:
    """A suspended turn whose evaluations can never back a resume ends its work, never the run.

    Core refuses such a resume (``ResumeRefused``) and the ask ends as a refused wait; every
    search must end that work and go on, never ask the suspended session again or wait forever.
    """
    shape = data.draw(st.fixed_dictionaries(SEARCHES[plugin_id].shapes), label="config")
    host = SUSPENDING[plugin_id](shape)
    host.run.adopting = False
    name = f"resume-refused({reason.value})"
    refused = next(index for index, fault in enumerate(FAULTS) if fault.name == name)
    try:
        play(host, [Step(0, 0, refused)] * MAX_STEPS)
    finally:
        note(host.run.timeline())
    assert "resume-refused" in host.run.timeline(), host.run.timeline()
