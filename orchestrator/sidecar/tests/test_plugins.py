"""Every launched session loads the vibesys plugin and none of the operator's own setup."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from agentshim import Agent, ApprovalPolicy, NativePermissions, TransportKind
from agentshim.testing import (
    ClaudeFramePeer,
    CodexScript,
    FakeClock,
    FakeExecutor,
    SequentialIds,
)

from tests import helpers as h
from vs_sidecar.driver import AgentshimDriver, build_session
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch

if TYPE_CHECKING:
    from vs_sidecar.gen.vibesys.orchestrator.v1 import agent_session_pb2 as sc

CONVERSATION = "11111111-1111-4111-8111-111111111111"
INIT = {"type": "system", "subtype": "init", "session_id": CONVERSATION, "model": "m"}
HARNESSES = [
    pytest.param(orch.HARNESS_CLAUDE_CODE, "claude", id="claude"),
    pytest.param(orch.HARNESS_CODEX, "codex", id="codex"),
]


def _agent(provider: str) -> tuple[Agent, FakeExecutor]:
    peers = (
        (lambda _request: ClaudeFramePeer([[INIT]])) if provider == "claude" else CodexScript().peer
    )
    executor = FakeExecutor([], peers=peers)
    agent = Agent(
        provider,
        transport=TransportKind.STREAM,
        model="fake-model",
        executor=executor,
        env={"PATH": "/usr/bin:/bin", "HOME": "/home/tester"},
        permissions=NativePermissions.bypass(),
        approvals=ApprovalPolicy.DENY,
        clock=FakeClock(),
        ids=SequentialIds(),
    )
    return agent, executor


def _open(request: sc.OpenSessionRequest, agent: Agent) -> str:
    """Open *request*'s session, close it, and return the conversation id it was given."""
    driver = AgentshimDriver(
        lambda conversation_id: build_session(
            request, agent=lambda _name, _environment: agent, new_conversation_id=conversation_id
        ),
        preferred_id=CONVERSATION,
    )
    opened = driver.open()
    driver.close()
    return opened.conversation_id


def _installs(executor: FakeExecutor) -> list[list[str]]:
    return [list(r.argv[1:]) for r in executor.requests if r.argv[1] == "plugin"]


@pytest.mark.parametrize(("harness", "provider"), HARNESSES)
def test_a_session_installs_and_loads_the_plugin_in_its_own_home(
    harness: int, provider: str
) -> None:
    request = h.open_request(harness)
    agent, executor = _agent(provider)
    _open(request, agent)
    marketplace, install = _installs(executor)
    assert marketplace == ["plugin", "marketplace", "add", request.skills.directory]
    assert install[-1] == f"{request.skills.name}@vibesys"
    spawn = executor.spawns[0]
    variable = "CLAUDE_CONFIG_DIR" if provider == "claude" else "CODEX_HOME"
    assert spawn.env[variable] == request.agent_data_directory
    argv = " ".join(spawn.argv)
    assert f"{request.skills.name}@vibesys" in argv


def test_claude_reads_only_project_settings_and_enables_only_the_plugin() -> None:
    request = h.open_request(orch.HARNESS_CLAUDE_CODE)
    agent, executor = _agent("claude")
    _open(request, agent)
    argv = executor.spawns[0].argv
    assert argv[argv.index("--setting-sources") + 1] == "project,local"
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["enabledPlugins"] == {"vibesys@vibesys": True}


def test_codex_loads_only_project_skills_and_the_plugin() -> None:
    request = h.open_request(orch.HARNESS_CODEX)
    agent, executor = _agent("codex")
    _open(request, agent)
    argv = executor.spawns[0].argv
    overrides = [argv[i + 1] for i, flag in enumerate(argv) if flag == "--config"]
    assert 'plugins={"vibesys@vibesys" = {enabled = true}}' in overrides
    assert "memories" in argv[argv.index("--disable") :]


@pytest.mark.parametrize(("harness", "provider"), HARNESSES)
def test_a_resume_reuses_the_home_and_installs_nothing_again(harness: int, provider: str) -> None:
    first = h.open_request(harness)
    agent, executor = _agent(provider)
    conversation = _open(first, agent)
    installs = _installs(executor)
    resumed = h.open_request(harness, resume_token=conversation.encode())
    resumed.session.session = 2
    assert resumed.skills == first.skills
    _open(resumed, agent)
    assert _installs(executor) == installs
    variable = "CLAUDE_CONFIG_DIR" if provider == "claude" else "CODEX_HOME"
    assert [spawn.env[variable] for spawn in executor.spawns] == [first.agent_data_directory] * 2
