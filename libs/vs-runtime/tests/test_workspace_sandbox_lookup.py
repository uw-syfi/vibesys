"""A turn's sandbox lookup names a missing sandbox instead of returning a default.

Regression for #1552: ``agent_sandbox_at`` answered ``None`` for a candidate
whose container was gone, the caller read that as "the root container serves
it", and the turn ran ``docker exec -w`` on a directory the root container does
not have (exit 127). Only the root workspace answers ``None`` now.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.workspace_world import open_workspace_env

from vs_runtime.api import AgentWorkspaceRouteError
from vs_sandbox.api.testing import FakeCommandRunner

if TYPE_CHECKING:
    from vs_runtime.api.infrastructure import RuntimeWorkspaces


@pytest.fixture(autouse=True)
def isolated_project_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "operator-state"))


@settings(
    max_examples=8,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    candidates=st.integers(min_value=1, max_value=3),
    released=st.sets(st.integers(min_value=0, max_value=2)),
    stray=st.lists(st.text(alphabet="abc/", min_size=1, max_size=8), max_size=3),
)
def test_only_the_root_and_live_candidates_have_an_answer(
    tmp_path_factory: pytest.TempPathFactory,
    candidates: int,
    released: set[int],
    stray: list[str],
) -> None:
    tmp_path = tmp_path_factory.mktemp("lookup")

    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        opened = [await workspaces.create_candidate() for _ in range(candidates)]
        paths = [candidate.path for candidate in opened]
        for index in sorted(released):
            if index < candidates:
                await opened[index].discard()
        # The root is served by the role's own sandbox: the one answer that is None.
        assert workspaces.agent_sandbox_at(workspaces.root.path) is None
        for index, path in enumerate(paths):
            if index in released:
                with pytest.raises(AgentWorkspaceRouteError, match=str(path)):
                    workspaces.agent_sandbox_at(path)
            else:
                assert isinstance(workspaces.agent_sandbox_at(path), FakeCommandRunner)
        for name in stray:
            unknown = Path("/nowhere") / name
            with pytest.raises(AgentWorkspaceRouteError):
                workspaces.agent_sandbox_at(unknown)

    with open_workspace_env(tmp_path) as env:
        try:
            asyncio.run(exercise(env.hosts[0]))
        finally:
            for host in reversed(env.hosts):
                with suppress(Exception):
                    asyncio.run(host.close())
