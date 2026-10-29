"""F17: publishing a retained candidate cannot require ownership of the operator checkout."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from tests.support.workspace_world import open_workspace_env

from vs_project.api.testing import FakeGitRepositories
from vs_sim.api.testing import InlineBlockingRunner

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.regression
@pytest.mark.parametrize("staged", [False, True])
def test_f17_adoption_preserves_operator_edits_after_host_resume(
    tmp_path: Path, *, staged: bool
) -> None:
    async def exercise() -> None:
        with open_workspace_env(tmp_path) as env:
            world = env.hosts[0]
            try:
                candidate = await world.create_candidate(member_id="winner")
                (candidate.path / "candidate.py").write_text("winner", encoding="utf-8")
                revision = await candidate.snapshot_and_retain(
                    "winner", retention_label="candidate"
                )
                await candidate.discard()
                disk = env.git_disk
                assert isinstance(disk, FakeGitRepositories)
                repository = disk.repository(world.root.path)
                file = world.root.path / "candidate.py"
                file.write_text("operator edit", encoding="utf-8")
                if staged:
                    repository.stage_all(["candidate.py"])
                before = repository.head()
                await world.close()
                world = env.start_host(InlineBlockingRunner())
                await world.adopt(revision)
                assert file.read_text() == "operator edit"
                assert repository.head() == before
                assert repository.has_staged_changes() is staged
            finally:
                for host in reversed(env.hosts):
                    await host.close()

    asyncio.run(exercise())
