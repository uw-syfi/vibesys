"""``vibesys control`` posts a validated request that the run then applies.

The command writes to the run's file inbox in its machine-local state; a run's control bridge reading
that inbox records the request as its own control. Invalid requests and unknown runs are
refused before anything is written.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from entrypoints.control import control_main, time_ordered_id
from vibesys.api import CONTROL_REQUEST
from vs_core.api import (
    CoreState,
    InputDropped,
    InputDropReason,
    InputMode,
    RoleId,
    RoleInputTarget,
    RunResultProposal,
    RunStatus,
    initial_state,
    parallel_limit,
    step,
)
from vs_project.api import (
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
    StoredEnvelope,
)
from vs_project.api.testing import run_execution_record
from vs_runtime.api.core import OperatorInbox, RunControlBridge
from vs_runtime.api.infrastructure import (
    ChannelRunControl,
    RunControlTransitionKind,
    create_run_control_channel,
)
from vs_runtime.api.testing import FakeRunControlEventSink
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
    from pathlib import Path

    from vs_core.api import ArtifactRef
    from vs_project.api import ControlInbox


def _project_with_run(root: Path, *, served: bool = True) -> str:
    """A run that started on this machine; ``served``: its loop declared its inbox format."""
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
    if served:
        project.state.control_inbox(manifest.run_id).serve()
    return manifest.run_id


def _record_core(root: Path, run_id: str, core: CoreState) -> None:
    """Commit ``core`` as the run's durable core record, as the run's loop would."""
    store = Project.open(root).state_store(run_id)
    fence = store.acquire("writer", now=0, duration=1)
    assert fence is not None
    payload = json.dumps({"envelope": {"core": core.model_dump(mode="json")}}).encode()
    store.commit(None, StoredEnvelope(revision=0, schema_version=1, payload=payload), fence, now=0)


def _terminal(core: CoreState) -> CoreState:
    result = RunResultProposal(outcome="cancelled", reason="stopped: by request")
    return core.model_copy(
        update={"run": core.run.model_copy(update={"status": RunStatus.TERMINAL, "result": result})}
    )


def _control(root: Path, argv: list[str], control_id: str = "r-1") -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = control_main(argv, cwd=root, new_control_id=lambda: control_id, out=out, err=err)
    return status, out.getvalue(), err.getvalue()


class _Texts:
    def put(self, ref: ArtifactRef, text: str) -> None:
        del ref, text


_COMMANDS = st.one_of(
    st.builds(lambda text: ["steer", text], st.text(min_size=1, max_size=20).filter(str.strip)),
    st.builds(
        lambda text, role: ["steer", text, "--role", role],
        st.text(min_size=1, max_size=20).filter(str.strip),
        st.sampled_from(["dynamic-planner", "dynamic-worker"]),
    ),
    st.just(["pause-now"]),
    st.just(["pause"]),
)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=25)
@given(command=_COMMANDS)
def test_a_posted_control_is_what_the_run_records(
    tmp_path_factory: pytest.TempPathFactory, command: list[str]
) -> None:
    root = tmp_path_factory.mktemp("project")
    run_id = _project_with_run(root)

    status, out, err = _control(root, [run_id, *command])

    warning = (
        "Unfinished work may need repeating and may still incur charges.\n"
        if command[0] == "pause-now"
        else ""
    )
    assert (status, out, err) == (0, "r-1\n", warning)
    (entry,) = Project.open(root).state.control_inbox(run_id).pending()
    request = CONTROL_REQUEST.validate_json(entry.payload)
    assert request.action == command[0].replace("-", "_")
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    if "--fallback" in command:
        # Only a turn waiting on a quota stop takes a fallback resume (wire-protocol.md).
        channel.enter_capacity()
        channel.pause_for_quota()
    inbox = OperatorInbox(
        Project.open(root).state.control_inbox(run_id), ChannelRunControl(channel, SimThreads())
    )
    bridge = RunControlBridge(channel, _Texts(), inbox=inbox)
    core = _declaring(("dynamic-planner", "dynamic-worker"))
    for event in bridge.poll(core, 1.0):
        core = step(core, event).state
    posted = [c for c in core.run.controls if c.control_id.root.startswith("op-")]
    assert [(c.control_id.root, c.action) for c in posted] == [
        ("op-r-1", command[0].replace("-", "_"))
    ]
    if "--role" in command:
        role = command[command.index("--role") + 1]
        assert posted[0].target == RoleInputTarget(role_id=RoleId(root=role))
        assert core.sessions.inputs[0].input.target == core.run.controls[0].target


def test_a_standing_steer_holds_until_vibesys_control_withdraws_it(tmp_path: Path) -> None:
    run_id = _project_with_run(tmp_path)
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    inbox = OperatorInbox(
        Project.open(tmp_path).state.control_inbox(run_id),
        ChannelRunControl(channel, SimThreads()),
    )
    bridge = RunControlBridge(channel, _Texts(), inbox=inbox)
    core = _declaring(("dynamic-planner",))

    assert _control(tmp_path, [run_id, "steer", "keep bf16", "--standing"], "r-1")[0] == 0
    for event in bridge.poll(core, 1.0):
        core = step(core, event).state
    (standing,) = core.sessions.inputs
    assert (standing.input.mode, standing.receipt) == (InputMode.STANDING, None)

    assert _control(tmp_path, [run_id, "withdraw", "r-1"], "r-2")[0] == 0
    for event in bridge.poll(core, 2.0):
        core = step(core, event).state
    assert [(c.control_id.root, c.action) for c in core.run.controls] == [
        ("op-r-1", "steer"),
        ("op-r-2", "withdraw"),
    ]
    (withdrawn,) = core.sessions.inputs
    assert isinstance(withdrawn.receipt, InputDropped)
    assert withdrawn.receipt.reason is InputDropReason.WITHDRAWN


@pytest.mark.parametrize(
    ("steer_id", "withdraw_id"),
    [("keep-bf16", "1760000000-abc"), ("1760000000-abc", "keep-bf16")],
    ids=["withdrawal-sorts-first", "steer-sorts-first"],
)
def test_a_withdrawal_posted_with_its_steer_ends_it_in_the_same_poll(
    tmp_path: Path, steer_id: str, withdraw_id: str
) -> None:
    """Regression: the inbox refused a withdrawal it read before its own steer, for good.

    The inbox is read in id order, and a time-ordered id sorts before a name like
    ``keep-bf16``; the withdrawal must still end the guidance its steer starts in that poll.
    """
    run_id = _project_with_run(tmp_path)
    sink = FakeRunControlEventSink()
    channel = create_run_control_channel(sink, threads=SimThreads())
    requests = Project.open(tmp_path).state.control_inbox(run_id)
    inbox = OperatorInbox(requests, ChannelRunControl(channel, SimThreads()))
    bridge = RunControlBridge(channel, _Texts(), inbox=inbox)
    core = _declaring(("dynamic-planner",))
    for event in bridge.poll(core, 0.5):
        core = step(core, event).state

    steer = [run_id, "--control-id", steer_id, "steer", "keep bf16", "--standing"]
    assert _control(tmp_path, steer)[0] == 0
    assert _control(tmp_path, [run_id, "withdraw", steer_id], withdraw_id)[0] == 0
    for now in (1.0, 2.0):
        for event in bridge.poll(core, now):
            core = step(core, event).state

    assert requests.rejected() == ()
    (withdrawn,) = core.sessions.inputs
    assert isinstance(withdrawn.receipt, InputDropped)
    assert withdrawn.receipt.reason is InputDropReason.WITHDRAWN
    ended = [t.text for t in sink.transitions if t.kind is RunControlTransitionKind.GUIDANCE_ENDED]
    assert ended == [f"standing guidance {steer_id} ended: withdrawn"]


def _production_bridge(
    tmp_path: Path, run_id: str
) -> tuple[RunControlBridge, ControlInbox, FakeRunControlEventSink]:
    """The bridge a run wires: its inbox and its socket share one ``ChannelRunControl``."""
    sink = FakeRunControlEventSink()
    channel = create_run_control_channel(sink, threads=SimThreads())
    control = ChannelRunControl(channel, SimThreads())
    requests = Project.open(tmp_path).state.control_inbox(run_id)
    bridge = RunControlBridge(
        channel, _Texts(), inbox=OperatorInbox(requests, control), control=control
    )
    return bridge, requests, sink


def test_a_withdrawal_that_ended_nothing_never_ends_guidance_sent_after_it(
    tmp_path: Path,
) -> None:
    """Regression: a withdrawal posted before its steer stayed pending in the inbox, so a
    restarted run read it again and ended the guidance the operator sent after it."""
    run_id = _project_with_run(tmp_path)
    bridge, requests, _sink = _production_bridge(tmp_path, run_id)
    core = _declaring(("dynamic-planner",))
    for event in bridge.poll(core, 0.5):
        core = step(core, event).state

    assert _control(tmp_path, [run_id, "withdraw", "keep"], "w-1")[0] == 0
    for event in bridge.poll(core, 1.0):
        core = step(core, event).state
    assert requests.pending() == ()
    ((rejected, reason),) = [(entry.request_id, entry.reason) for entry in requests.rejected()]
    assert rejected == "w-1"
    assert "ended nothing" in reason

    steer = [run_id, "--control-id", "keep", "steer", "keep bf16", "--standing"]
    assert _control(tmp_path, steer)[0] == 0
    for event in bridge.poll(core, 2.0):
        core = step(core, event).state
    restarted, _, _ = _production_bridge(tmp_path, run_id)
    for now in (3.0, 4.0):
        for event in restarted.poll(core, now):
            core = step(core, event).state

    (guidance,) = core.sessions.inputs
    assert (guidance.input.mode, guidance.receipt) == (InputMode.STANDING, None)
    assert all(control.action != "withdraw" for control in core.run.controls)


def test_an_action_posted_by_vibesys_control_applies_or_is_set_aside(tmp_path: Path) -> None:
    """A new parallel limit is recorded as the run's control and reported applied; a cancel of
    an attempt the run does not hold changes nothing, so core's rule refuses it, its request is
    set aside with core's reason, reported once, and a restarted run never reads it again."""
    run_id = _project_with_run(tmp_path)
    bridge, requests, sink = _production_bridge(tmp_path, run_id)
    core = _declaring(("dynamic-planner",))
    for event in bridge.poll(core, 0.5):
        core = step(core, event).state

    assert _control(tmp_path, [run_id, "set-max-parallel", "1"], "l-1")[0] == 0
    assert _control(tmp_path, [run_id, "cancel-attempt", "A-9"], "c-1")[0] == 0
    for event in bridge.poll(core, 1.0):
        core = step(core, event).state
    bridge.settled(core, running=False)

    assert [(c.control_id.root, c.action, c.max_parallel) for c in core.run.controls] == [
        ("op-l-1", "set_max_parallel", 1)
    ]
    assert parallel_limit(core.run) == 1
    reason = "refused by the run: the run holds no such attempt or item"
    assert [(entry.request_id, entry.reason) for entry in requests.rejected()] == [("c-1", reason)]
    told = [
        (t.kind, t.request_id, t.text)
        for t in sink.transitions
        if t.kind
        in (RunControlTransitionKind.REQUEST_REJECTED, RunControlTransitionKind.ACTION_APPLIED)
    ]
    assert told == [
        (RunControlTransitionKind.REQUEST_REJECTED, "c-1", reason),
        (RunControlTransitionKind.ACTION_APPLIED, None, "set_max_parallel 1"),
    ]
    assert _control(tmp_path, [run_id, "status", "l-1"], "s-1")[1].strip() == "applied"
    restarted, _, _ = _production_bridge(tmp_path, run_id)
    assert restarted.poll(core, 2.0) == ()
    assert requests.pending() == ()


def _declaring(roles: tuple[str, ...]) -> CoreState:
    state = initial_state()
    declaration = state.run.declaration.model_copy(
        update={"roles": tuple(RoleId(root=role) for role in roles)}
    )
    return state.model_copy(
        update={"run": state.run.model_copy(update={"declaration": declaration})}
    )


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

    status, out, err = _control(tmp_path / "project", [run_id, "pause-now"])

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


def test_status_says_what_became_of_each_request(tmp_path: Path) -> None:
    """A request reads pending until the run applies it, or rejected with the run's reason."""
    run_id = _project_with_run(tmp_path)
    bridge, _requests, sink = _production_bridge(tmp_path, run_id)
    assert _control(tmp_path, [run_id, "pause"], "p-1")[0] == 0
    assert _control(tmp_path, [run_id, "withdraw", "nothing"], "w-1")[0] == 0

    assert _control(tmp_path, [run_id, "status", "p-1"]) == (0, "pending\n", "")
    core = _declaring(("dynamic-planner",))
    for now in (1.0, 2.0):
        for event in bridge.poll(core, now):
            core = step(core, event).state

    assert _control(tmp_path, [run_id, "status", "p-1"]) == (0, "applied\n", "")
    status, out, err = _control(tmp_path, [run_id, "status", "w-1"])
    assert (status, err) == (0, "")
    assert out.startswith("rejected: it ended nothing")
    rejected = [
        t.summary for t in sink.transitions if t.kind is RunControlTransitionKind.REQUEST_REJECTED
    ]
    assert rejected == [f"control request w-1 rejected: {out.removeprefix('rejected: ').strip()}"]
    status, out, err = _control(tmp_path, [run_id, "status", "never-sent"])
    assert (status, out) == (1, "")
    assert "never-sent" in err


def test_an_ended_run_refuses_a_control_before_writing_it(tmp_path: Path) -> None:
    """Regression: posting to a run that had ended exited 0 and the request never applied.

    The run's core record is the one fact for its end: no marker in the inbox is needed, so
    a run that died before setting its inbox aside, or ended before this check existed, is
    refused all the same, and what it never applied reads as rejected, not pending.
    """
    run_id = _project_with_run(tmp_path)
    _bridge, requests, _sink = _production_bridge(tmp_path, run_id)
    assert _control(tmp_path, [run_id, "steer", "too late"], "s-1")[0] == 0
    _record_core(tmp_path, run_id, _terminal(_declaring(("dynamic-planner",))))

    status, out, err = _control(tmp_path, [run_id, "pause"], "p-1")

    assert (status, out) == (3, "")
    assert "has ended (the run ended: stopped: by request)" in err
    assert requests.status("p-1", ended=None) is None
    status, out, _ = _control(tmp_path, [run_id, "status", "s-1"])
    assert status == 0
    assert out == (
        "rejected: the run ended before applying it: the run ended: stopped: by request\n"
    )


def test_a_retry_of_an_applied_control_after_the_run_ended_reports_it_applied(
    tmp_path: Path,
) -> None:
    """``--control-id`` is for a retry after an uncertain write: it stays one request."""
    run_id = _project_with_run(tmp_path)
    bridge, _requests, _sink = _production_bridge(tmp_path, run_id)
    assert _control(tmp_path, ["--control-id", "stop-1", run_id, "pause-now"])[0] == 0
    core = _declaring(("dynamic-planner",))
    for now in (1.0, 2.0):  # applied at the first poll, settled once core holds it
        for event in bridge.poll(core, now):
            core = step(core, event).state
    _record_core(tmp_path, run_id, _terminal(core))

    assert _control(tmp_path, ["--control-id", "stop-1", run_id, "pause-now"]) == (
        0,
        "stop-1\n",
        "Unfinished work may need repeating and may still incur charges.\n",
    )
    assert _control(tmp_path, [run_id, "status", "stop-1"]) == (0, "applied\n", "")


def test_a_post_to_a_run_that_has_not_declared_posting_order_warns(tmp_path: Path) -> None:
    run_id = _project_with_run(tmp_path, served=False)

    status, out, err = _control(tmp_path, [run_id, "pause"], "p-1")

    assert (status, out) == (0, "p-1\n")
    assert "has not declared that it applies requests in posting order" in err


def test_pause_posts_to_the_allow_list_and_settles_after_the_durable_drain(tmp_path: Path) -> None:
    run_id = _project_with_run(tmp_path)
    project = Project.open(tmp_path)
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    inbox = project.state.control_inbox(run_id)
    bridge = RunControlBridge(
        channel, _Texts(), inbox=OperatorInbox(inbox, ChannelRunControl(channel, SimThreads()))
    )
    assert _control(tmp_path, [run_id, "pause"], "park-1")[0] == 0
    state = initial_state()
    for event in bridge.poll(state, 1.0):
        state = step(state, event).state
    bridge.settled(state, running=False)
    assert state.run.status == RunStatus.PARKED
    assert state.run.result is None
    assert _control(tmp_path, [run_id, "status", "park-1"])[1].strip() == "applied"
