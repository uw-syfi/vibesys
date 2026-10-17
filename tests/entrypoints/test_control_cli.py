"""``vibesys control`` posts a validated request that the run then applies.

The command writes to the run's file inbox in its machine-local state; a run's control bridge reading
that inbox records the request as its own control. Invalid requests and unknown runs are
refused before anything is written.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from entrypoints.control import control_main, time_ordered_id
from vibesys.api import CONTROL_REQUEST
from vs_core.api import initial_state, step
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord
from vs_project.api.testing import run_execution_record
from vs_runtime.api.core import OperatorInbox, RunControlBridge
from vs_runtime.api.infrastructure import ChannelRunControl, create_run_control_channel
from vs_runtime.api.testing import FakeRunControlEventSink
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
    from pathlib import Path

    from vs_core.api import ArtifactRef


def _project_with_run(root: Path) -> str:
    project = Project.open(root)
    project.state.create_project("control", now=datetime(2026, 1, 1, tzinfo=UTC))
    manifest = project.state.new_run_manifest(
        "control",
        branch="test",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="dynamic", config_version=1, options={}),
        trusted_input_baseline="a" * 40,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    project.state.create_run(manifest)
    with project.state.exclusive_run_host(manifest.run_id):  # the run started on this machine
        pass
    return manifest.run_id


def _control(root: Path, argv: list[str], control_id: str = "r-1") -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = control_main(argv, cwd=root, new_control_id=lambda: control_id, out=out, err=err)
    return status, out.getvalue(), err.getvalue()


class _Texts:
    def put(self, ref: ArtifactRef, text: str) -> None:
        del ref, text


_COMMANDS = st.one_of(
    st.builds(lambda text: ["steer", text], st.text(min_size=1, max_size=20).filter(str.strip)),
    st.just(["pause"]),
    st.just(["resume"]),
    st.just(["resume", "--fallback"]),
    st.sampled_from([["stop"], ["stop", "--mode", "cancel"], ["stop", "--mode", "drain"]]),
)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=25)
@given(command=_COMMANDS)
def test_a_posted_control_is_what_the_run_records(
    tmp_path_factory: pytest.TempPathFactory, command: list[str]
) -> None:
    root = tmp_path_factory.mktemp("project")
    run_id = _project_with_run(root)

    status, out, err = _control(root, [run_id, *command])

    assert (status, out, err) == (0, "r-1\n", "")
    (entry,) = Project.open(root).state.control_inbox(run_id).pending()
    request = CONTROL_REQUEST.validate_json(entry.payload)
    assert request.action == command[0]
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    inbox = OperatorInbox(
        Project.open(root).state.control_inbox(run_id), ChannelRunControl(channel, SimThreads())
    )
    bridge = RunControlBridge(channel, _Texts(), inbox=inbox)
    core = initial_state()
    for event in bridge.poll(core, 1.0):
        core = step(core, event).state
    assert [(c.control_id.root, c.action) for c in core.run.controls] == [("op-r-1", command[0])]


@pytest.mark.parametrize(
    ("argv", "named"),
    [
        (["{run}", "steer", ""], "text"),
        (["no-such-run", "pause"], "no-such-run"),
        (["{run}", "--control-id", "../x", "pause"], "control_id"),
    ],
)
def test_an_invalid_control_is_refused_and_nothing_is_written(
    tmp_path: Path, argv: list[str], named: str
) -> None:
    run_id = _project_with_run(tmp_path)

    status, out, err = _control(tmp_path, [arg.format(run=run_id) for arg in argv])

    assert (status, out) == (2, "")
    assert named in err
    assert Project.open(tmp_path).state.control_inbox(run_id).pending() == ()


def test_a_pending_id_cannot_be_posted_again_with_another_request(tmp_path: Path) -> None:
    run_id = _project_with_run(tmp_path)
    assert _control(tmp_path, [run_id, "steer", "prefer latency"])[0] == 0
    assert _control(tmp_path, [run_id, "steer", "prefer latency"])[0] == 0

    status, out, err = _control(tmp_path, [run_id, "steer", "prefer throughput"])

    assert (status, out) == (2, "")
    assert "different request" in err
    (entry,) = Project.open(tmp_path).state.control_inbox(run_id).pending()
    assert b"prefer latency" in entry.payload


def test_a_control_under_another_state_home_is_refused_and_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: posting under another state home exited 0 and the run never saw it."""
    (tmp_path / "project").mkdir()
    run_id = _project_with_run(tmp_path / "project")
    other = tmp_path / "other-home"
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(other))

    status, out, err = _control(tmp_path / "project", [run_id, "stop"])

    assert (status, out) == (2, "")
    assert str(other.resolve()) in err
    assert "VIBESYS_STATE_HOME" in err
    assert not any(other.rglob("*.json"))


def test_fallback_and_a_capacity_wait_cannot_be_combined(tmp_path: Path) -> None:
    run_id = _project_with_run(tmp_path)
    with pytest.raises(SystemExit):
        _control(tmp_path, [run_id, "resume", "--fallback", "--when", "capacity_returns"])


@given(st.floats(0, 4e9), st.floats(0, 4e9))
def test_time_ordered_ids_sort_by_time(earlier: float, later: float) -> None:
    earlier, later = sorted((earlier, later))
    if round(later * 1000) > round(earlier * 1000):
        assert time_ordered_id(earlier, "ffff") < time_ordered_id(later, "0000")
