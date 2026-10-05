"""Session executors enforce each role's workspace access on real Git, across crashes."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.executor_context import context_for
from tests.support.session_lifecycle_world import (
    cancel_request,
    close_request,
    lifecycle_executor,
    open_lifecycle_host,
    resume_request,
)
from tests.support.session_world import (
    SessionHost,
    dispatch_request,
    ensure_request,
    inspect_request,
    run_snapshot,
)
from tests.support.workspace_world import RUN_ID, WorkspaceEnv, open_workspace_env

from vs_agent.api import AgentSessionState, DurableSessionStore
from vs_core.api import Access, ObservationStatus
from vs_runtime.api import RuntimeContractError
from vs_runtime.api.core import (
    AccessGuardedWorkspace,
    ExecutionResult,
    JournalRunInvocations,
    ReceiptStore,
    ReleasedRunInvocations,
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
class Probe:
    """What the world saw of the workspace across every host restart."""

    restores: int = 0
    """Times a revert ran: the access policy applied to the tree."""
    calls: int = 0
    """Provider turns that ran."""
    seen: list[dict[str, str]] = field(default_factory=list)
    """The tree as the provider saw it at the start of each turn."""


class Watched:
    """The real workspace, counting reverts: a Fake of the boundary, not a patch."""

    def __init__(self, inner: RuntimeWorkspace, probe: Probe) -> None:
        self._inner = inner
        self._probe = probe
        self.access_recovery = inner.access_recovery
        self.path = inner.path

    async def snapshot(self, label: str) -> str:
        return await self._inner.snapshot(label)

    async def pending_changes(self) -> list[str]:
        return await self._inner.pending_changes()

    async def restore_for_agent(self, revision: str, *, preserve_paths: tuple[str, ...]) -> None:
        self._probe.restores += 1
        await self._inner.restore_for_agent(revision, preserve_paths=preserve_paths)


@dataclass
class AccessWorld:
    """A real Git run workspace, a Fake provider that writes into it, and restartable hosts."""

    env: WorkspaceEnv
    answer: dict[str, object] | None = None
    access: Access = Access.READ_ONLY
    grant_paths: tuple[str, ...] = ()
    host: SessionHost = field(init=False)
    writes: dict[str, str] = field(default_factory=dict)
    probe: Probe = field(default_factory=Probe)

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
        sessions = self.env.project.state.local_namespace(RUN_ID, "sessions")
        host = open_lifecycle_host(
            self.root.path,
            DurableSessionStore(sessions.slot("sessions.json", AgentSessionState)),
            answer=self.answer,
            effect=self.write,
        )
        host.resolver.guarded = Watched(self.root, self.probe)
        host.resolver.access = self.access
        host.resolver.grant_paths = self.grant_paths
        return host

    def write(self) -> None:
        self.probe.calls += 1
        self.probe.seen.append(tree(self.root.path))
        for name, text in self.writes.items():
            target = self.root.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    def store(self) -> ReceiptStore:
        return ReceiptStore(self.env.receipts_namespace())

    async def execute(self, request: RequestBase) -> ExecutionResult:
        return await self.host.run(request, self.store())

    async def route(self, request: RequestBase) -> ExecutionResult:
        """Execute any session request, lifecycle kinds included, on a freshly started host."""
        executor = lifecycle_executor(self.host, self.store())
        outcome = await executor.execute(cast("Any", request), context_for(request))
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


class Role(StrEnum):
    READ_ONLY = "read-only"
    LIMITED = "limited"
    READ_WRITE = "read-write"


class Writes(StrEnum):
    NONE = "none"
    INSIDE = "inside-allowed-paths"
    OUTSIDE = "outside-allowed-paths"


class Outcome(StrEnum):
    COMPLETED = "completed"
    INVALID = "invalid"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"
    CLOSED = "closed"
    RESUMED = "resumed"


class Restart(StrEnum):
    NONE = "none"
    BEFORE_SETTLE = "before-settle"
    AFTER_SETTLE = "after-settle"
    SNAPSHOT_FIRST = "snapshot-first"
    """Die before settling, restart, and snapshot before any executor handles a request."""


ALLOWED = "allowed.txt"
WRITTEN = {
    Writes.NONE: {},
    Writes.INSIDE: {ALLOWED: "inside\n"},
    Writes.OUTSIDE: {"elsewhere/c.txt": "outside\n"},
}
ACCESS = {
    Role.READ_ONLY: Access.READ_ONLY,
    Role.LIMITED: Access.WRITE_ARTIFACTS,
    Role.READ_WRITE: Access.WRITE_CANDIDATE,
}
SETTLES_ITSELF = {Outcome.COMPLETED, Outcome.INVALID}
ENDED = {Outcome.COMPLETED, Outcome.INVALID, Outcome.CANCELLED, Outcome.CLOSED}


def authorized(role: Role, path: str) -> bool:
    return role is Role.READ_WRITE or (role is Role.LIMITED and path == ALLOWED)


async def play(world: AccessWorld, outcome: Outcome, restart: Restart) -> None:
    """Run one invocation to *outcome*, crashing and restarting as asked, then recover it.

    The provider writes ``world.writes`` during invocation ``inv-1`` only.
    """
    writes = world.writes
    world.writes = {}
    await world.route(ensure_request())
    if outcome is Outcome.RESUMED:
        await world.route(dispatch_request("req-0", "inv-0"))  # a conversation to resume
    world.writes = writes
    ends_here = {
        Outcome.COMPLETED: dispatch_request("req-1", "inv-1"),
        Outcome.INVALID: dispatch_request("req-1", "inv-1"),
        Outcome.UNKNOWN: inspect_request("req-i", "inv-1"),
        Outcome.CANCELLED: cancel_request("req-c", "inv-1"),
        Outcome.CLOSED: close_request("req-x"),
        Outcome.RESUMED: resume_request("req-r", "inv-2"),
    }[outcome]
    if outcome not in SETTLES_ITSELF:
        world.host.faults.down = True  # accepted, wrote, then the provider died
        await world.route(dispatch_request("req-1", "inv-1"))
        world.host.faults.down = False
        world.writes = {}
    if restart in {Restart.BEFORE_SETTLE, Restart.SNAPSHOT_FIRST}:
        dying = DiesBeforeRevert(world.root)
        world.host.resolver.guarded = cast("AccessGuardedWorkspace", dying)
        with contextlib.suppress(HostDied):
            await world.route(ends_here)
    else:
        await world.route(ends_here)
    if restart is not Restart.NONE:
        world.restart()
        if restart is Restart.SNAPSHOT_FIRST:
            lifecycle_executor(world.host, world.store())  # the restarted host is up, idle
            frozen = tree(world.root.path)
            with pytest.raises(RuntimeContractError):
                await world.root.snapshot("before-any-executor")
            assert tree(world.root.path) == frozen
        await world.route(ends_here)
        await world.route(inspect_request("req-i2", "inv-1"))


@pytest.mark.parametrize("restart", list(Restart))
@pytest.mark.parametrize("outcome", list(Outcome))
@pytest.mark.parametrize("writes", list(Writes))
@pytest.mark.parametrize("role", list(Role))
def test_access_is_settled_once_when_the_writer_is_proven_ended_and_never_baselined_before(
    tmp_path: Path, role: Role, writes: Writes, outcome: Outcome, restart: Restart
) -> None:
    async def run(world: AccessWorld) -> None:
        world.access = ACCESS[role]
        world.grant_paths = (ALLOWED,) if role is Role.LIMITED else ()
        world.answer = {"wrong": 1} if outcome is Outcome.INVALID else None
        world.host = world.open()
        before = tree(world.root.path)
        world.writes = dict(WRITTEN[writes])
        await play(world, outcome, restart)

        written = set(WRITTEN[writes])
        violating = {name for name in written if not authorized(role, name)}
        kept = written - violating
        proof = ReleasedRunInvocations(
            JournalRunInvocations(world.host.sessions(), world.store()),
            world.host.sessions(),
            world.store(),
        )
        proven = proof.unproven(run_snapshot("inv-1")) is None
        after = tree(world.root.path)
        changed = {name for name in after if after[name] != before.get(name)}
        # A writer proven ended has had its writes judged: only authorized ones remain.
        if proven:
            assert changed == kept
            assert world.probe.restores == (1 if violating else 0)
        # An authorized write is never reverted, whatever happened to the turn.
        assert kept <= changed
        assert world.probe.restores <= 1
        # The ends the lifecycle proves are exactly the ones that were judged.
        if outcome in ENDED and outcome is not Outcome.CLOSED:
            assert proven
        if outcome is Outcome.UNKNOWN:
            assert not proven

        # Whatever happened, no snapshot adopts an unjudged tree: it is refused or clean.
        try:
            await world.root.snapshot("probe")
        except RuntimeContractError:
            assert not proven
        else:
            still = tree(world.root.path)
            assert {name for name in violating if still.get(name) != before.get(name)} == set()

        # The next turn never starts from an unjudged tree either.
        calls = world.probe.calls
        world.writes = {}
        await world.route(dispatch_request("req-probe", "inv-9"))
        if world.probe.calls > calls:
            seen = world.probe.seen[-1]
            assert {name for name in violating if seen.get(name) != before.get(name)} == set()

    with open_workspace_env(tmp_path) as env:
        world = AccessWorld(env)
        asyncio.run(run(world))
        for workspaces in env.hosts:
            asyncio.run(workspaces.close())


@pytest.mark.asyncio
async def test_a_read_only_writer_that_dies_cannot_make_its_writes_the_baseline(
    world: AccessWorld,
) -> None:
    """The review's repro: write, then ConnectionError, then restart and inspect."""
    before = tree(world.root.path)
    world.writes = {"candidate.py": "VALUE = 99\n"}
    await world.route(ensure_request())
    world.host.faults.down = True
    lost = await world.route(dispatch_request())
    assert status(lost) is ObservationStatus.UNKNOWN
    assert await world.root.pending_changes() == ["candidate.py"]

    world.restart()
    inspected = await world.route(inspect_request())
    assert inspected.observation.target is not None
    assert inspected.observation.target.observation.status is ObservationStatus.UNKNOWN
    with pytest.raises(RuntimeContractError):
        await world.root.snapshot("next")
    assert await world.root.pending_changes() == ["candidate.py"]  # still judged, not adopted

    world.host.faults.down = False
    cancelled = await world.route(cancel_request())
    assert status(cancelled) is ObservationStatus.CANCELLED
    assert tree(world.root.path) == before
    assert await world.root.snapshot("next")  # the fence lifted with the settlement
    assert await world.root.pending_changes() == []


@pytest.mark.asyncio
async def test_a_resume_judges_the_turn_a_crash_left_unsettled_before_its_own_baseline(
    world: AccessWorld,
) -> None:
    before = tree(world.root.path)
    await world.route(ensure_request())
    await world.route(dispatch_request("req-0", "inv-0"))  # a conversation to resume
    world.writes = {"candidate.py": "VALUE = 99\n"}
    dying = DiesBeforeRevert(world.root)
    world.host.resolver.guarded = cast("AccessGuardedWorkspace", dying)
    with pytest.raises(HostDied):
        await world.route(dispatch_request("req-1", "inv-1"))
    world.restart()
    world.writes = {}
    resumed = await world.route(resume_request("req-r", "inv-2"))
    assert status(resumed) is ObservationStatus.SUCCEEDED
    assert world.probe.seen[-1] == before  # the resumed turn started from the judged tree
    assert tree(world.root.path) == before
    assert world.probe.restores == 1
