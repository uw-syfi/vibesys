"""A container sandbox as agentshim's confinement: which container, marked, reaped."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

from agentshim.testing import FakeExecutor, FakeRun
from hypothesis import given
from hypothesis import strategies as st
from tests.support.fake_docker_sandbox import FakeDockerSandbox

from vs_agent.docker_confinement import DockerSandboxConfinement

if TYPE_CHECKING:
    from vs_sandbox.api import DockerSandbox

_WORKSPACE = Path("/srv/workspace")
CONTAINER_IDS = st.text(alphabet="0123456789abcdef", min_size=4, max_size=12)
ENV_NAMES = st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ_", min_size=1, max_size=12).filter(
    lambda name: name not in {"HOME", "PATH"}
)


def _confinement(sandbox: FakeDockerSandbox, runner: FakeExecutor) -> DockerSandboxConfinement:
    return DockerSandboxConfinement(cast("DockerSandbox", sandbox), runner=runner)


@given(first=CONTAINER_IDS, second=CONTAINER_IDS, argv=st.lists(st.text(min_size=1), min_size=1))
def test_every_wrap_and_reap_targets_the_containers_current_id(
    first: str, second: str, argv: list[str]
) -> None:
    sandbox = FakeDockerSandbox(workspace=_WORKSPACE, container_id=first)
    runner = FakeExecutor(FakeRun())
    confinement = _confinement(sandbox, runner)

    wrapped = confinement.wrap(argv, "/workspace")
    assert first in wrapped
    assert wrapped[-len(argv) :] == argv

    # A GPU reselect replaces the container; nothing rebuilds the confinement.
    sandbox.container_id = second
    assert second in confinement.wrap(argv, "/workspace")
    confinement.reap()
    assert runner.requests[-1].argv[:3] == ["docker", "exec", second]


@given(names=st.lists(ENV_NAMES, unique=True, min_size=1, max_size=4), value=st.text(min_size=1))
def test_the_environment_travels_by_name_and_the_process_is_marked(
    names: list[str], value: str
) -> None:
    sandbox = FakeDockerSandbox(workspace=_WORKSPACE, extra_env=dict.fromkeys(names, value))
    confinement = _confinement(sandbox, FakeExecutor(FakeRun()))

    argv = confinement.wrap(["claude"], "/workspace")

    flags = [argv[i + 1] for i, part in enumerate(argv) if part == "-e"]
    assert "AGENTSHIM_CONFINED=1" in flags
    assert set(names) <= set(flags)
    assert all("=" not in flag for flag in flags if flag != "AGENTSHIM_CONFINED=1")
    assert dict(confinement.env) == dict(sandbox.env)


def test_agent_paths_follow_the_sandboxs_own_mapping(tmp_path: Path) -> None:
    sandbox = FakeDockerSandbox(workspace=tmp_path)
    confinement = _confinement(sandbox, FakeExecutor(FakeRun()))

    assert confinement.agent_path(tmp_path / "src" / "a.py") == "/workspace/src/a.py"
    assert confinement.agent_path("/usr/bin/python3") == "/usr/bin/python3"
