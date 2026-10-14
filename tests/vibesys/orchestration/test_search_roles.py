"""Every built-in plugin declares exactly the agents its search asks.

A search declares its roles once (`Role`, listed in `@search(roles=...)`), and the host adds an
`AgentRole` per agent (system prompt, tools, capabilities) in the plugin's `agents`. The runtime
refuses a turn whose agent the plugin does not declare only when that turn is first asked, so a
role added to a search without its host agent would fail mid-run. A role that puts another
question to an existing agent (`Role.agent`) needs no agent of its own. A role that may wait for
evaluations must be able to submit them, so its agent carries the evaluation tool. A new
built-in fails `test_every_built_in_names_its_search` until it is listed in `SEARCHES`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vibesys.api.catalog import BUILT_INS
from vibesys.orchestration.dynamic.search.api import dynamic
from vibesys.orchestration.evolve.search.api import evolve
from vibesys.orchestration.issue_queue.search.api import issue_queue
from vibesys.orchestration.multi.search.api import multi
from vibesys.orchestration.single.search.api import single
from vs_runtime.api import AgentTool
from vs_runtime.api.core import EVALUATION_TOOL_ID

if TYPE_CHECKING:
    from vibesys.plugin_registration import OrchestrationRegistration

SEARCHES = {
    "single-agent": single,
    "profile-guided-single-agent": single,
    "multi-agent": multi,
    "profile-guided-multi-agent": multi,
    "dynamic": dynamic,
    "evolve": evolve,
    "plain": issue_queue,
}
"""The search each built-in plugin runs, by plugin id."""


def test_every_built_in_names_its_search() -> None:
    assert sorted(SEARCHES) == sorted(item.plugin.id for item in BUILT_INS)


@pytest.mark.parametrize("registration", BUILT_INS, ids=lambda item: item.plugin.id)
def test_a_plugin_declares_one_agent_per_agent_its_search_asks(
    registration: OrchestrationRegistration,
) -> None:
    search = SEARCHES[registration.plugin.id]
    asked = sorted({search.ids.role_id(role.identity).root for role in search.roles})
    assert sorted(agent.id for agent in registration.plugin.agents) == asked


@pytest.mark.parametrize("registration", BUILT_INS, ids=lambda item: item.plugin.id)
def test_an_agent_whose_role_may_wait_for_evaluations_carries_the_evaluation_tool(
    registration: OrchestrationRegistration,
) -> None:
    search = SEARCHES[registration.plugin.id]
    waiting = {
        search.ids.role_id(role.identity).root
        for role in search.roles
        if role.may_wait_for_evaluations
    }
    carrying = {
        agent.id
        for agent in registration.plugin.agents
        if AgentTool(id=EVALUATION_TOOL_ID) in agent.extra_tools
    }
    assert waiting <= carrying
