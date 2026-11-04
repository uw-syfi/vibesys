"""Every launched session loads the vibesys plugin and none of the operator's own setup."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
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
from google.protobuf import json_format
from hypothesis import given
from hypothesis import strategies as st

from tests import helpers as h
from vs_sidecar.driver import AgentshimDriver, build_session
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch
from vs_sidecar.main import LaunchError, load_launch

if TYPE_CHECKING:
    from vs_sidecar.gen.vibesys.orchestrator.v1 import effects_pb2 as fx

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


def _open(launch: fx.LaunchSession, agent: Agent) -> str:
    """Open *launch*'s session, close it, and return the conversation id it was given."""
    driver = AgentshimDriver(
        lambda conversation_id: build_session(
            launch, agent=lambda _name, _identity: agent, new_conversation_id=conversation_id
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
    launch = h.launch(harness)
    agent, executor = _agent(provider)
    _open(launch, agent)
    marketplace, install = _installs(executor)
    assert marketplace == ["plugin", "marketplace", "add", launch.plugin.marketplace]
    assert install[-1] == f"{launch.plugin.name}@vibesys"
    spawn = executor.spawns[0]
    variable = "CLAUDE_CONFIG_DIR" if provider == "claude" else "CODEX_HOME"
    assert spawn.env[variable] == launch.plugin.home
    argv = " ".join(spawn.argv)
    assert f"{launch.plugin.name}@vibesys" in argv


def test_claude_reads_only_project_settings_and_enables_only_the_plugin() -> None:
    launch = h.launch(orch.HARNESS_CLAUDE_CODE)
    agent, executor = _agent("claude")
    _open(launch, agent)
    argv = executor.spawns[0].argv
    assert argv[argv.index("--setting-sources") + 1] == "project,local"
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["enabledPlugins"] == {"vibesys@vibesys": True}


def test_codex_loads_only_project_skills_and_the_plugin() -> None:
    launch = h.launch(orch.HARNESS_CODEX)
    agent, executor = _agent("codex")
    _open(launch, agent)
    argv = executor.spawns[0].argv
    overrides = [argv[i + 1] for i, flag in enumerate(argv) if flag == "--config"]
    assert 'plugins={"vibesys@vibesys" = {enabled = true}}' in overrides
    assert "memories" in argv[argv.index("--disable") :]


@pytest.mark.parametrize(("harness", "provider"), HARNESSES)
def test_a_resume_reuses_the_home_and_installs_nothing_again(harness: int, provider: str) -> None:
    first = h.launch(harness)
    agent, executor = _agent(provider)
    conversation = _open(first, agent)
    installs = _installs(executor)
    resumed = h.launch(harness, resume_conversation_id=conversation)
    resumed.session.session = 2
    assert resumed.plugin == first.plugin
    _open(resumed, agent)
    assert _installs(executor) == installs
    variable = "CLAUDE_CONFIG_DIR" if provider == "claude" else "CODEX_HOME"
    assert [spawn.env[variable] for spawn in executor.spawns] == [first.plugin.home] * 2


@pytest.mark.parametrize(("harness", "provider"), HARNESSES)
def test_a_launch_without_a_plugin_starts_no_agent(harness: int, provider: str) -> None:
    launch = h.launch(harness)
    launch.ClearField("plugin")
    agent, executor = _agent(provider)
    with pytest.raises(ValueError, match=r"launch\.plugin"):
        build_session(launch, agent=lambda _name, _identity: agent)
    assert executor.requests == []


def _launch_file(tmp_path: Path, launch: fx.LaunchSession) -> Path:
    path = tmp_path / "launch.json"
    path.write_text(json_format.MessageToJson(launch))
    return path


def test_the_launch_file_round_trips_the_plugin(tmp_path: Path, plugin_root: Path) -> None:
    launch = h.launch()
    loaded = load_launch(_launch_file(tmp_path, launch), None)
    assert loaded.plugin == launch.plugin
    assert loaded.plugin.home == str(plugin_root / "plugin-home")


def test_a_launch_file_without_a_plugin_is_refused_naming_the_field(tmp_path: Path) -> None:
    launch = h.launch()
    launch.ClearField("plugin")
    with pytest.raises(LaunchError, match=r"launch\.plugin is missing"):
        load_launch(_launch_file(tmp_path, launch), None)


def test_a_marketplace_without_a_manifest_is_refused_naming_the_field(tmp_path: Path) -> None:
    launch = h.launch()
    launch.plugin.marketplace = str(tmp_path / "nowhere")
    with pytest.raises(LaunchError, match=r"launch\.plugin\.marketplace .*nowhere"):
        load_launch(_launch_file(tmp_path, launch), None)


def test_a_manifest_that_is_not_json_is_refused_naming_the_field(tmp_path: Path) -> None:
    launch = h.launch()
    (tmp_path / "market" / ".claude-plugin").mkdir(parents=True)
    (tmp_path / "market" / ".claude-plugin" / "marketplace.json").write_text("{")
    launch.plugin.marketplace = str(tmp_path / "market")
    with pytest.raises(LaunchError, match=r"launch\.plugin\.marketplace"):
        load_launch(_launch_file(tmp_path, launch), None)


@given(name=st.from_regex(r"[a-z][a-z0-9-]{0,20}", fullmatch=True).filter(lambda n: n != "vibesys"))
def test_a_plugin_the_marketplace_does_not_hold_is_refused_naming_the_field(name: str) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        launch = h.launch()
        launch.plugin.name = name
        with pytest.raises(LaunchError, match=r"launch\.plugin\.name"):
            load_launch(_launch_file(root, launch), None)


@given(path=st.text(min_size=1, max_size=30).filter(lambda p: not p.startswith("/")))
@pytest.mark.parametrize("field", ["marketplace", "home"])
def test_a_relative_plugin_path_is_refused_naming_the_field(field: str, path: str) -> None:
    with tempfile.TemporaryDirectory() as directory:
        launch = h.launch()
        setattr(launch.plugin, field, path)
        with pytest.raises(LaunchError, match=field):
            load_launch(_launch_file(Path(directory), launch), None)
