"""Every built-in search ends cleanly whatever core does to its waits and however it is stopped.

Each built-in orchestration registers how its host starts (`HOSTS`), and the shared fault menu of
`tests.vibesys.search_faults` does the rest: a generated story answers waits the search's own way
or with any fault core can deliver, the operator may stop (draining or cancelling) at any point,
and the run must still end in one `Finish` with every attempt settled. A new built-in fails
`test_every_built_in_search_is_played_under_faults` until it registers here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given, note, settings
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
from tests.vibesys.search_faults import play, steps, stops
from tests.vibesys.serial_search._replies import attribution
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.api.catalog import BUILT_INS
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.vibesys.search_faults import SearchHost, Step, Stop

PROFILE_GUIDED = {"command": ("bash", "attribute.sh"), "timeout_seconds": 10}

HOSTS: dict[str, Callable[[bool], SearchHost]] = {
    "dynamic": lambda restarting: dynamic_begin(
        dynamic_config(max_rounds=2, max_in_flight=2), restarting=restarting
    ),
    "evolve": lambda restarting: evolve_begin(
        evolve_config(max_generations=2, children_per_generation=2, max_parallelism=2),
        restarting=restarting,
    ),
    "plain": lambda restarting: queue_begin(
        queue_config(), restarting=restarting, view=suspending_view(), suspending=True
    ),
    "single-agent": lambda restarting: SingleHost.start(
        single_config(max_rounds=2), restarting=restarting
    ),
    "profile-guided-single-agent": lambda restarting: SingleHost.start(
        single_config(max_rounds=2, attribution=PROFILE_GUIDED),
        restarting=restarting,
        attribution_output=attribution(),
    ),
    "multi-agent": lambda restarting: MultiHost.start(
        multi_config(max_rounds=2), restarting=restarting, validating=True, suspending=True
    ),
    "profile-guided-multi-agent": lambda restarting: MultiHost.start(
        multi_config(max_rounds=2, attribution=PROFILE_GUIDED),
        restarting=restarting,
        attribution_output=attribution(),
    ),
}
"""How each built-in orchestration's host starts, by plugin id."""


def test_every_built_in_search_is_played_under_faults() -> None:
    assert sorted(HOSTS) == sorted(item.plugin.id for item in BUILT_INS)


@pytest.mark.parametrize("plugin_id", sorted(HOSTS))
@across_restarts
@settings(max_examples=examples(pr=4, nightly=80), deadline=None, derandomize=True, database=None)
@given(story=steps(), stop=stops())
def test_a_search_ends_cleanly_whatever_core_does_to_its_waits_and_however_it_stops(
    *, plugin_id: str, restarting: bool, story: list[Step], stop: Stop | None
) -> None:
    host = HOSTS[plugin_id](restarting)
    host.run.adopting = False
    try:
        play(host, story, stop)
    finally:
        note(host.run.timeline())
