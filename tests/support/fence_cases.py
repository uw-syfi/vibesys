"""The setup/release pairs the effect-fence contract runs (``vs_runtime.api.fence_contracts``).

Each pair is one executor's create and release requests over real disk and Fakes:

* ``session-close``: EnsureSession binds a session, CloseSession releases it;
* ``turn-cancel``: DispatchTurn calls the provider, CancelTurn stops or withdraws the turn;
* ``workspace-discard``: EnsureWorkspace creates a worktree, DiscardWorkspace removes it.

A new executor that creates and releases adds its pair to ``FENCE_PAIRS``.
"""

from __future__ import annotations

import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from tests.support.executor_cases import state_home
from tests.support.session_lifecycle_world import (
    cancel_request,
    close_request,
    lifecycle_executor,
    open_lifecycle_host,
    released_keys,
)
from tests.support.session_world import (
    SettledRunInvocations,
    dispatch_request,
    ensure_request,
)
from tests.support.workspace_world import WorkspaceEnv, open_workspace_env

from vs_agent.api import AgentSessionState, DurableSessionStore, Pending
from vs_core.api import (
    AttemptId,
    AttemptRef,
    DecisionId,
    DiscardWorkspace,
    EnsureWorkspace,
    RequestId,
    Scope,
    WorkspaceMode,
    WorkspacePlan,
)
from vs_project.api import Project
from vs_project.api.testing import world_git
from vs_runtime.api.core import (
    ExecutionResult,
    ReceiptStore,
    RuntimeWorkspaceRequests,
    SessionBinding,
    owner_key,
    revision_ref,
)
from vs_runtime.api.executor_contexts import context_for

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from tests.support.session_world import SessionHost

    from vs_core.api import RequestBase
    from vs_project.api import GitRepository, StateNamespace
    from vs_runtime.api.fence_contracts import FenceHost, FenceWorld, HostLife
    from vs_sim.api import BlockingRunner

_INVOCATION = "inv-1"


@dataclass
class _Host:
    """One host: an executor and the epoch its context carries."""

    executor: Any
    epoch: int

    async def execute(self, request: RequestBase) -> ExecutionResult:
        outcome = await self.executor.execute(
            cast("Any", request), context_for(request, epoch=self.epoch)
        )
        assert isinstance(outcome, ExecutionResult), outcome
        return outcome


# sessions


class _SessionWorld:
    """The lifecycle session world; the pair decides which requests set up and release."""

    def __init__(self, base: Path) -> None:
        (base / "project").mkdir()
        (base / "workspace").mkdir()
        self._project = Project.open(base / "project")
        slot = self._project.state.state_store_namespace("sessions").slot(
            "sessions.json", AgentSessionState
        )
        self.session_host: SessionHost = open_lifecycle_host(
            base / "workspace", DurableSessionStore(slot)
        )

    def _namespace(self) -> StateNamespace:
        return self._project.state.state_store_namespace("run")

    def store(self) -> ReceiptStore:
        return ReceiptStore(self._namespace())

    def host(self, epoch: int, life: HostLife, blocking: BlockingRunner) -> FenceHost:
        store = ReceiptStore(life.namespace(self._namespace()))
        return _Host(lifecycle_executor(self.session_host, store, blocking=blocking), epoch)

    def bound(self) -> bool:
        key = f"{owner_key(ensure_request())}/{ensure_request().spec.session_id.root}"
        return self.store().load("session-bindings", "binding", key, SessionBinding) is not None


class _SessionCloseWorld(_SessionWorld):
    async def prepare(self, host: FenceHost) -> None:
        del host

    def setup(self, identity: str) -> RequestBase:
        return ensure_request(f"req-ensure-{identity}")

    def release(self, identity: str) -> RequestBase:
        return close_request(f"req-close-{identity}")

    def created(self) -> int:
        return int(self.bound())

    def held(self) -> int:
        return int(self.bound() and released_keys(self.session_host) == 0)


class _TurnCancelWorld(_SessionWorld):
    async def prepare(self, host: FenceHost) -> None:
        ensured = await host.execute(ensure_request())
        assert ensured.observation.observation.accepted, ensured

    def setup(self, identity: str) -> RequestBase:
        return dispatch_request(f"req-dispatch-{identity}", _INVOCATION)

    def release(self, identity: str) -> RequestBase:
        return cancel_request(f"req-cancel-{identity}", invocation=_INVOCATION)

    def created(self) -> int:
        return len(self.session_host.turns)

    def held(self) -> int:
        """A provider turn whose dispatch call has not returned and was never released."""
        state = self.session_host.journal.load_optional()
        row = None if state is None else state.invocations.get(_INVOCATION)
        return int(row is not None and isinstance(row.outcome, Pending) and not row.interrupted)


@dataclass
class _SessionPair:
    name: str
    world_type: type[_SessionCloseWorld | _TurnCancelWorld]

    @asynccontextmanager
    async def world(self) -> AsyncIterator[FenceWorld]:
        with tempfile.TemporaryDirectory() as raw:
            yield self.world_type(Path(raw))


# workspaces

_ATTEMPT = AttemptRef(attempt_id=AttemptId(root="a1"), generation=0)


def _attempt_fields(name: str) -> dict[str, Any]:
    return {
        "request_id": RequestId(root=name),
        "scope": Scope(owner=_ATTEMPT.attempt_id, generation=_ATTEMPT.generation),
        "admission_id": DecisionId(root="admit"),
        "attempt": _ATTEMPT,
    }


class _CountingGit:
    """A repository that counts the worktrees it was asked to add."""

    def __init__(self, git: GitRepository, added: list[Path]) -> None:
        self._git = git
        self._added = added

    def __getattr__(self, name: str) -> object:
        return getattr(self._git, name)

    def add_worktree(self, destination: Path, commit: str) -> None:
        self._added.append(destination)
        self._git.add_worktree(destination, commit)


class _WorkspaceWorld:
    def __init__(self, env: WorkspaceEnv, added: list[Path]) -> None:
        self._env = env
        self._added = added
        base = env.hosts[0].root.revision
        assert base is not None
        self._plan = WorkspacePlan(mode=WorkspaceMode.ISOLATED_CHILD, base=revision_ref(base))

    async def prepare(self, host: FenceHost) -> None:
        del host

    def setup(self, identity: str) -> RequestBase:
        return EnsureWorkspace(**_attempt_fields(f"ensure-{identity}"), plan=self._plan)

    def release(self, identity: str) -> RequestBase:
        return DiscardWorkspace(**_attempt_fields(f"discard-{identity}"))

    def host(self, epoch: int, life: HostLife, blocking: BlockingRunner) -> FenceHost:
        store = ReceiptStore(life.namespace(self._env.receipts_namespace()))
        workspaces = self._env.start_host(blocking)
        executor = RuntimeWorkspaceRequests(workspaces, store, SettledRunInvocations(), blocking)
        return _Host(cast("Any", executor), epoch)

    def created(self) -> int:
        return len(self._added)

    def held(self) -> int:
        root = self._env.hosts[0].root.path.resolve()
        git = world_git(root, self._env.git_disk)
        return len([path for path in git.worktrees() if path != root])


class _WorkspacePair:
    name = "workspace-discard"

    @asynccontextmanager
    async def world(self) -> AsyncIterator[FenceWorld]:
        added: list[Path] = []

        def counting(git: GitRepository) -> GitRepository:
            return cast("GitRepository", _CountingGit(git, added))

        with (
            tempfile.TemporaryDirectory() as raw,
            state_home(Path(raw) / "operator-state"),
            open_workspace_env(Path(raw), wrap_git=counting) as env,
        ):
            try:
                yield _WorkspaceWorld(env, added)
            finally:
                for host in reversed(env.hosts):
                    await host.close()


FENCE_PAIRS = (
    _SessionPair("session-close", _SessionCloseWorld),
    _SessionPair("turn-cancel", _TurnCancelWorld),
    _WorkspacePair(),
)

__all__ = ["FENCE_PAIRS"]
