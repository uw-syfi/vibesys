"""A captured command runs in a throwaway workspace at a retained revision, and only there."""

from __future__ import annotations

from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import CaptureCommandBase, RevisionId, RevisionRef
from vs_runtime.api import CommandResult
from vs_runtime.api.core import CaptureCommandOwner
from vs_runtime.api.testing import FakeCommands, FakeWorkspace, FakeWorkspaces


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
        FakeWorkspace(known_revisions=retained), supports_parallel_candidates=parallel
    )
    commands = FakeCommands()
    return CaptureCommandOwner(workspaces, commands, workspaces, _commit_of), workspaces, commands


@given(retained=st.sets(st.sampled_from(["r1", "r2"])), asked=st.sampled_from(["r1", "r2", "x"]))
async def test_only_a_retained_revision_is_captured_and_its_candidate_is_always_discarded(
    retained: set[str], asked: str
) -> None:
    owner, workspaces, commands = _owner(retained)
    commands.script(CommandResult(output="bottlenecks", exit_code=0))
    outcome = await owner.capture(_request(asked))
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
    outcome = await owner.capture(_request("r1"))
    assert (outcome["status"], outcome["exit_code"], outcome["output"]) == ("succeeded", 2, "boom")


async def test_an_infrastructure_failure_propagates_and_still_discards_the_candidate() -> None:
    owner, workspaces, commands = _owner({"r1"})
    commands.script(RuntimeError("sandbox lost"))
    with pytest.raises(RuntimeError, match="sandbox lost"):
        await owner.capture(_request("r1"))
    assert workspaces.candidates[0].discarded


async def test_a_run_without_isolated_candidates_refuses_instead_of_touching_the_root() -> None:
    owner, workspaces, commands = _owner({"r1"}, parallel=False)
    outcome = await owner.capture(_request("r1"))
    assert outcome["status"] == "rejected"
    assert not workspaces.candidates
    assert not commands.calls
