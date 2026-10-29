"""A run-scoped turn's artifact directory lives in the run root and outlives restores.

A run-scoped `WRITE_ARTIFACTS` turn (a role that a plugin lets write below a configured
directory, `profile/` here) writes in the run root and a later turn reads what it wrote. That
needs (1) the turn's writes under the configured artifact directory to be kept and anything else
reverted, and (2) the file to be there when the next turn starts, whatever workspace work
happened between the two turns.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from tests.support.session_world import dispatch_request, ensure_request
from tests.support.workspace_world import open_workspace_env

from vs_core.api import Access, ObservationStatus

from .test_session_access import AccessWorld, status, tree

if TYPE_CHECKING:
    from pathlib import Path


EVIDENCE = "profile/plan.md"


async def _run_scoped_turn(world: AccessWorld, request_id: str, invocation: str) -> None:
    result = await world.execute(dispatch_request(request_id, invocation))
    assert status(result) is ObservationStatus.SUCCEEDED


def test_the_artifact_written_by_one_run_scoped_turn_is_there_at_the_next(tmp_path: Path) -> None:
    async def run(world: AccessWorld) -> None:
        world.host.resolver.access = Access.WRITE_ARTIFACTS
        world.host.resolver.grant_paths = ("profile",)
        world.host.resolver.grant_directories = ("profile",)
        world.writes = {EVIDENCE: "step 1\n", "elsewhere.txt": "not allowed\n"}
        await world.execute(ensure_request())
        first = await world.execute(dispatch_request("req-1", "inv-1"))
        assert status(first) is ObservationStatus.FAILED  # the write outside the grant
        world.writes = {EVIDENCE: "step 1\n"}
        await _run_scoped_turn(world, "req-2", "inv-2")
        world.writes = {}
        await _run_scoped_turn(world, "req-3", "inv-3")
        assert world.probe.seen[-1][EVIDENCE] == tree(world.root.path)[EVIDENCE]
        assert (world.root.path / EVIDENCE).read_text(encoding="utf-8") == "step 1\n"
        assert "elsewhere.txt" not in tree(world.root.path)

    with open_workspace_env(tmp_path) as env:
        world = AccessWorld(env)
        asyncio.run(run(world))
        for workspaces in env.hosts:
            asyncio.run(workspaces.close())


def test_the_artifact_survives_a_restore_of_the_root_between_two_run_scoped_turns(
    tmp_path: Path,
) -> None:
    async def run(world: AccessWorld) -> None:
        world.host.resolver.access = Access.WRITE_ARTIFACTS
        world.host.resolver.grant_paths = ("profile",)
        world.host.resolver.grant_directories = ("profile",)
        world.writes = {EVIDENCE: "step 1\n"}
        await world.execute(ensure_request())
        await _run_scoped_turn(world, "req-1", "inv-1")
        baseline = world.root.trusted_input_baseline
        assert baseline is not None
        await world.root.restore(baseline)
        assert (world.root.path / EVIDENCE).read_text(encoding="utf-8") == "step 1\n"

    with open_workspace_env(tmp_path, memory_paths=("profile",)) as env:
        world = AccessWorld(env)
        asyncio.run(run(world))
        for workspaces in env.hosts:
            asyncio.run(workspaces.close())
