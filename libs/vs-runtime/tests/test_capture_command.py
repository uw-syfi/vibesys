"""A captured command runs in a throwaway workspace at a retained revision, and only there."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import CaptureCommandBase, RevisionId, RevisionRef, Until
from vs_runtime.api import CommandResult, Workspace
from vs_runtime.api.core import CaptureCommandOwner, ExecutionContext, NotApplied
from vs_runtime.api.testing import FakeCommands, FakeWorkspace, FakeWorkspaces
from vs_sim.api.testing import arrival


class Capture(CaptureCommandBase):
    kind: Literal["demo.capture_command"] = "demo.capture_command"


def _ref(name: str) -> RevisionRef:
    return RevisionRef(revision_id=RevisionId(root=name), digest=f"git-commit:{name}")


def _commit_of(ref: RevisionRef) -> str | None:
    root = ref.revision_id.root
    return root if ref.digest == f"git-commit:{root}" else None


def _request(name: str) -> Capture:
    return Capture(
        revision=_ref(name), argv=("profile", "--fast"), output_argument="--out", timeout_seconds=30
    )


def _owner(
    retained: set[str], *, parallel: bool = True
) -> tuple[CaptureCommandOwner, FakeWorkspaces, FakeCommands]:
    workspaces = FakeWorkspaces(
        FakeWorkspace(path=Path("/workspace"), known_revisions=retained),
        supports_parallel_candidates=parallel,
    )
    commands = FakeCommands()
    return CaptureCommandOwner(workspaces, commands, workspaces, _commit_of), workspaces, commands


@given(retained=st.sets(st.sampled_from(["r1", "r2"])), asked=st.sampled_from(["r1", "r2", "x"]))
async def test_only_a_retained_revision_is_captured_and_its_candidate_is_always_discarded(
    retained: set[str], asked: str
) -> None:
    owner, workspaces, commands = _owner(retained)
    commands.script(CommandResult(output="bottlenecks", exit_code=0))
    outcome = await owner.capture(_request(asked), "id")
    if asked not in retained:
        assert outcome["status"] == "rejected"
        assert not workspaces.candidates
        assert not commands.calls
        return
    assert outcome == {
        "status": "succeeded",
        "exit_code": 0,
        "output": "bottlenecks",
        "truncated": False,
    }
    (candidate,) = workspaces.candidates
    (call,) = commands.calls
    assert call.workspace is candidate
    assert (call.argv, call.output_argument, call.timeout_seconds) == (
        ("profile", "--fast"),
        "--out",
        30,
    )
    assert candidate.discarded


async def test_a_command_that_exits_nonzero_is_reported_not_raised() -> None:
    owner, _, commands = _owner({"r1"})
    commands.script(CommandResult(output="boom", exit_code=2))
    outcome = await owner.capture(_request("r1"), "id")
    assert (outcome["status"], outcome["exit_code"], outcome["output"]) == ("succeeded", 2, "boom")


async def test_a_failed_capture_exposes_only_its_prebounded_diagnostic() -> None:
    """Attribution cannot recover a secret split by the raw stream's prefix/tail cap."""
    owner, _, commands = _owner({"r1"})
    secret = "s" * 240
    commands.script(
        CommandResult(
            output=f"prefix API_TOKEN={secret[:20]}...[truncated]...{secret[-20:]} tail",
            exit_code=7,
            truncated=True,
            diagnostic_output="prefix API_TOKEN=[REDACTED] tail",
        )
    )

    outcome = await owner.capture(_request("r1"), "id")

    assert outcome["output"] == "prefix API_TOKEN=[REDACTED] tail"
    assert secret[:20] not in str(outcome["output"])
    assert secret[-20:] not in str(outcome["output"])


async def test_a_successful_capture_keeps_raw_protocol_output() -> None:
    owner, _, commands = _owner({"r1"})
    commands.script(
        CommandResult(
            output="protocol-token",
            exit_code=0,
            diagnostic_output="[REDACTED]",
        )
    )

    outcome = await owner.capture(_request("r1"), "id")

    assert outcome["output"] == "protocol-token"


async def test_an_infrastructure_failure_propagates_and_still_discards_the_candidate() -> None:
    owner, workspaces, commands = _owner({"r1"})
    commands.script(RuntimeError("sandbox lost"))
    with pytest.raises(RuntimeError, match="sandbox lost"):
        await owner.capture(_request("r1"), "id")
    assert workspaces.candidates[0].discarded


async def test_a_run_without_isolated_candidates_refuses_instead_of_touching_the_root() -> None:
    owner, workspaces, commands = _owner({"r1"}, parallel=False)
    outcome = await owner.capture(_request("r1"), "id")
    assert outcome["status"] == "rejected"
    assert not workspaces.candidates
    assert not commands.calls


class _HangingCommands(FakeCommands):
    """A command runner whose process never returns: the host dies while it runs."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def capture_output(
        self,
        argv: tuple[str, ...],
        *,
        workspace: Workspace,
        output_argument: str,
        timeout_seconds: int | None = None,
    ) -> CommandResult:
        del argv, workspace, output_argument, timeout_seconds
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError


@given(identity=st.text(alphabet="0123456789abcdef", min_size=8, max_size=64))
async def test_a_host_that_dies_mid_capture_leaves_nothing_after_the_replay(identity: str) -> None:
    """Regression: the candidate had a fresh path, so the dead host's worktree was never reaped.

    The replay under the same identity takes over the member's path, which replaces the
    leftover; whatever the identity, no unowned candidate survives the replay.
    """
    retained = {"r1"}
    workspaces = FakeWorkspaces(
        FakeWorkspace(path=Path("/workspace"), known_revisions=retained),
        supports_parallel_candidates=True,
    )
    hanging = _HangingCommands()
    dying = CaptureCommandOwner(workspaces, hanging, workspaces, _commit_of)
    first = asyncio.ensure_future(dying.capture(_request("r1"), identity))
    try:
        await arrival(hanging.started.wait(), first)
        workspaces.host_died()
        assert len(workspaces.leaked_candidates()) == 1
        commands = FakeCommands()
        commands.script(CommandResult(output="bottlenecks", exit_code=0))
        restarted = CaptureCommandOwner(workspaces, commands, workspaces, _commit_of)
        outcome = await restarted.capture(_request("r1"), identity)
        assert outcome["status"] == "succeeded"
        assert workspaces.leaked_candidates() == ()
        assert all(candidate.discarded for candidate in workspaces.candidates)
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


async def test_inspection_never_runs_the_command() -> None:
    owner, _, commands = _owner({"r1"})
    context = ExecutionContext(
        now_at=0.0,
        payload_digest="d",
        until=Until(at=100.0),
    )
    verdict = await owner.inspect(_request("r1"), context)
    assert isinstance(verdict, NotApplied)
    assert not commands.calls
