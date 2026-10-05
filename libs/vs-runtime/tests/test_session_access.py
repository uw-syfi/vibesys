"""Session executors enforce each role's workspace access on real Git, across crashes."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.executor_context import context_for
from tests.support.session_world import (
    SessionHost,
    dispatch_request,
    ensure_request,
    inspect_request,
    open_host,
    run_snapshot,
)
from tests.support.workspace_world import WorkspaceEnv, open_workspace_env

from vs_core.api import Access, ObservationStatus
from vs_runtime.api.core import (
    AccessGuardedWorkspace,
    ExecutionResult,
    JournalRunInvocations,
    ReceiptStore,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from vs_core.api import RequestBase
    from vs_runtime.api.infrastructure import RuntimeWorkspace


@pytest.fixture(autouse=True)
def isolated_project_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "operator-state"))


def tree(root: Path) -> dict[str, str]:
    """Every file below *root* outside Git metadata, by relative path and content digest."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


class HostDied(Exception):  # noqa: N818  # lint-waiver: LW-837232 [N818]; names the simulated event, not an error condition.
    """The process died at this point."""


class DiesBeforeRevert:
    """A workspace whose host dies when the revert first looks at the tree."""

    def __init__(self, inner: RuntimeWorkspace) -> None:
        self._inner = inner
        self.armed = True
        self.access_recovery = inner.access_recovery
        self.path = inner.path

    async def snapshot(self, label: str) -> str:
        return await self._inner.snapshot(label)

    async def pending_changes(self) -> list[str]:
        if self.armed:
            raise HostDied
        return await self._inner.pending_changes()

    async def restore_for_agent(self, revision: str, *, preserve_paths: tuple[str, ...]) -> None:
        await self._inner.restore_for_agent(revision, preserve_paths=preserve_paths)


@dataclass
class AccessWorld:
    """A real Git run workspace, a Fake provider that writes into it, and restartable hosts."""

    env: WorkspaceEnv
    host: SessionHost = field(init=False)
    writes: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.host = self.open()

    @property
    def root(self) -> RuntimeWorkspace:
        return self.env.hosts[-1].root

    def restart(self) -> None:
        """A new process: fresh workspaces and recovery state, the same disk and journal."""
        previous = self.host
        self.env.start_host()
        self.host = self.open()
        self.host.journal, self.host.client = previous.journal, previous.client

    def open(self) -> SessionHost:
        """A host whose provider writes ``self.writes`` into the real workspace each turn."""
        host = open_host(self.root.path, effect=self.write)
        host.resolver.guarded = self.root
        return host

    def write(self) -> None:
        for name, text in self.writes.items():
            target = self.root.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    def store(self) -> ReceiptStore:
        return ReceiptStore(self.env.receipts_namespace())

    async def execute(self, request: RequestBase) -> ExecutionResult:
        outcome = await self.host.executor(self.store()).execute(
            cast("Any", request), context_for(request)
        )
        assert isinstance(outcome, ExecutionResult), outcome
        return outcome


@pytest.fixture
def world(tmp_path: Path) -> Iterator[AccessWorld]:
    with open_workspace_env(tmp_path) as env:
        access = AccessWorld(env)
        yield access
        for workspaces in env.hosts:
            asyncio.run(workspaces.close())


def status(result: ExecutionResult) -> ObservationStatus:
    return result.observation.observation.status


@pytest.mark.asyncio
async def test_a_read_only_turn_leaves_the_workspace_byte_identical_and_reports_the_violation(
    world: AccessWorld,
) -> None:
    before = tree(world.root.path)
    world.writes = {"candidate.py": "VALUE = 99\n", "notes/new.txt": "hello\n"}
    await world.execute(ensure_request())
    result = await world.execute(dispatch_request())
    assert tree(world.root.path) == before
    assert status(result) is ObservationStatus.FAILED
    assert result.observation.observation.accepted
    assert result.observation.observation.terminal
    assert result.observation.outcome_json is None
    assert "candidate.py" in result.observation.observation.diagnostic
    assert "notes/new.txt" in result.observation.observation.diagnostic
    assert await world.root.pending_changes() == []


@pytest.mark.asyncio
async def test_a_turn_that_writes_nothing_succeeds_untouched(world: AccessWorld) -> None:
    before = tree(world.root.path)
    await world.execute(ensure_request())
    result = await world.execute(dispatch_request())
    assert status(result) is ObservationStatus.SUCCEEDED
    assert tree(world.root.path) == before


@pytest.mark.asyncio
async def test_a_role_with_write_access_keeps_its_writes(world: AccessWorld) -> None:
    world.host.resolver.access = Access.WRITE_CANDIDATE
    world.writes = {"candidate.py": "VALUE = 2\n"}
    await world.execute(ensure_request())
    result = await world.execute(dispatch_request())
    assert status(result) is ObservationStatus.SUCCEEDED
    assert (world.root.path / "candidate.py").read_text(encoding="utf-8") == "VALUE = 2\n"


@pytest.mark.asyncio
async def test_a_crash_after_the_turn_before_the_revert_is_reverted_on_replay(
    world: AccessWorld,
) -> None:
    before = tree(world.root.path)
    world.writes = {"candidate.py": "VALUE = 99\n"}
    await world.execute(ensure_request())
    dying = DiesBeforeRevert(world.root)
    world.host.resolver.guarded = cast("AccessGuardedWorkspace", dying)
    with pytest.raises(HostDied):
        await world.execute(dispatch_request())
    assert tree(world.root.path) != before  # the turn's write is on disk, unreverted
    assert len(world.host.turns) == 1

    world.restart()
    proof = JournalRunInvocations(world.host.sessions(), world.store())
    assert proof.unproven(run_snapshot()) is not None  # not provable until reverted
    replayed = await world.execute(dispatch_request())
    assert tree(world.root.path) == before
    assert status(replayed) is ObservationStatus.FAILED
    assert "candidate.py" in replayed.observation.observation.diagnostic
    assert world.host.turns == []  # replayed from the journal, never dispatched again
    assert proof.unproven(run_snapshot()) is None


@pytest.mark.asyncio
async def test_inspection_after_a_crash_also_reverts_and_reports_the_violation(
    world: AccessWorld,
) -> None:
    before = tree(world.root.path)
    world.writes = {"candidate.py": "VALUE = 99\n"}
    await world.execute(ensure_request())
    dying = DiesBeforeRevert(world.root)
    world.host.resolver.guarded = cast("AccessGuardedWorkspace", dying)
    with pytest.raises(HostDied):
        await world.execute(dispatch_request())
    world.restart()
    inspected = await world.execute(inspect_request())
    assert tree(world.root.path) == before
    assert inspected.observation.target is not None
    assert inspected.observation.target.observation.status is ObservationStatus.FAILED
    assert inspected.observation.target.observation.accepted


NAMES = st.sampled_from(
    ["candidate.py", "allowed.txt", "other.txt", "out/a.txt", "out/deep/b.txt", "elsewhere/c.txt"]
)


@settings(
    max_examples=12, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(written=st.sets(NAMES, max_size=4))
def test_a_limited_role_keeps_only_the_paths_its_grant_names(
    tmp_path_factory: pytest.TempPathFactory, written: set[str]
) -> None:
    async def run(world: AccessWorld) -> None:
        world.host.resolver.access = Access.WRITE_ARTIFACTS
        world.host.resolver.grant_paths = ("allowed.txt", "out")
        world.host.resolver.grant_directories = ("out",)
        world.writes = dict.fromkeys(sorted(written), "changed\n")
        before = tree(world.root.path)
        await world.execute(ensure_request())
        result = await world.execute(dispatch_request())
        allowed = {"allowed.txt", "out/a.txt", "out/deep/b.txt"}
        kept = {name for name in written if name in allowed}
        after = tree(world.root.path)
        assert {name for name in after if after[name] != before.get(name)} == kept
        violating = written - allowed
        if violating:
            assert status(result) is ObservationStatus.FAILED
            assert all(name in result.observation.observation.diagnostic for name in violating)
        else:
            assert status(result) is ObservationStatus.SUCCEEDED

    with open_workspace_env(tmp_path_factory.mktemp("access")) as env:
        world = AccessWorld(env)
        asyncio.run(run(world))
        for workspaces in env.hosts:
            asyncio.run(workspaces.close())
