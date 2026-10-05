"""The production builder gives every session executor the one access settlement and fence."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import pytest
from tests.support.executor_context import context_for
from tests.support.session_lifecycle_world import cancel_request
from tests.support.session_world import (
    ROLE,
    SCHEMA,
    Reply,
    dispatch_request,
    ensure_request,
)
from tests.support.workspace_world import RUN_ID, WorkspaceEnv, open_workspace_env

from vs_agent.api import (
    AgentBackend,
    AgentClient,
    AgentExecutionPolicy,
    AgentSessionSpec,
    AgentSpec,
    AgentTurnRequest,
)
from vs_agent.api.testing import FakeAgentInvocationStore, FakeDriver
from vs_core.api import ArtifactId, ArtifactRef, CancelTurn, DispatchTurn, ObservationStatus
from vs_runtime.api import AgentRole, ArtifactStore, RuntimeContractError, WorkspaceAccess
from vs_runtime.api.core import (
    ExecutionResult,
    ReceiptStore,
    ResolverInputs,
    SessionExecutors,
    StoreWorkspaceReceipts,
    open_session_requests,
)
from vs_runtime.api.infrastructure import AgentExecutionConfiguration

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from vs_core.api import RequestBase

WRITTEN = "candidate.py"


@pytest.fixture(autouse=True)
def isolated_project_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "operator-state"))


@dataclass
class Provider:
    """A read-only role's provider that overwrites a file in the workspace, then may die."""

    env: WorkspaceEnv
    dies: bool = False
    turns: list[AgentTurnRequest] = field(default_factory=list)

    def on_turn(self, turn: AgentTurnRequest) -> None:
        self.turns.append(turn)
        (self.env.hosts[-1].root.path / WRITTEN).write_text("VALUE = 99\n", encoding="utf-8")
        if self.dies:
            message = "provider died after writing"
            raise ConnectionError(message)


@dataclass
class Run:
    """One run's disk state; ``start`` builds a host's executors only through the builder."""

    env: WorkspaceEnv
    provider: Provider
    journal: FakeAgentInvocationStore = field(default_factory=FakeAgentInvocationStore)
    executors: SessionExecutors = field(init=False)
    artifacts: ArtifactStore = field(init=False)

    def __post_init__(self) -> None:
        self.start()

    def start(self) -> None:
        """A host: fresh workspaces and executors over the same disk and journal."""
        if len(self.env.hosts) > 1 or hasattr(self, "executors"):
            self.env.start_host()
        store = ReceiptStore(self.env.receipts_namespace())
        artifacts = ArtifactStore(self.env.project.state.local_namespace(RUN_ID, "artifacts"))
        self.artifacts = artifacts

        def spec(selected: AgentRole, path: Path) -> AgentSessionSpec:
            return AgentSessionSpec(
                role=selected.id,
                provider="fake",
                workspace=path,
                policy=AgentExecutionPolicy(require_enforcement=False),
            )

        inputs = ResolverInputs(
            roles=(
                AgentRole(
                    id=ROLE.root,
                    system_prompt="You are a worker.",
                    workspace_access=WorkspaceAccess.READ_ONLY,
                ),
            ),
            schemas={SCHEMA: Reply},
            workspaces=self.env.hosts[-1],
            workspace_receipts=StoreWorkspaceReceipts(store),
            artifacts=artifacts,
            configuration=lambda selected: AgentExecutionConfiguration(
                selected.id, AgentSpec(backend=AgentBackend.STUB, cli_timeout=45)
            ),
            session_spec=spec,
        )
        client = AgentClient(FakeDriver(answer={"value": 7}, on_turn=self.provider.on_turn))
        self.executors = open_session_requests(
            inputs, client=client, invocation_slot=self.journal, store=store
        )

    def dispatch(self) -> DispatchTurn:
        """The default turn, with a prompt that exists in the artifact store."""
        receipt = self.artifacts.write(b"Work.")
        prompt = ArtifactRef(
            artifact_id=ArtifactId(root=f"artifact-{receipt.sha256[:8]}"), digest=receipt.sha256
        )
        request = dispatch_request()
        return request.model_copy(
            update={"turn": request.turn.model_copy(update={"prompts": (prompt,)})}
        )

    @property
    def path(self) -> Path:
        return self.env.hosts[-1].root.path

    async def execute(self, request: RequestBase) -> ExecutionResult:
        executor = (
            self.executors.lifecycle if isinstance(request, CancelTurn) else self.executors.turns
        )
        outcome = await executor.execute(cast("Any", request), context_for(request))
        assert isinstance(outcome, ExecutionResult), outcome
        return outcome


def digest(path: Path) -> str:
    return hashlib.sha256((path / WRITTEN).read_bytes()).hexdigest()


@pytest.fixture
def run(tmp_path: Path) -> Iterator[Run]:
    with open_workspace_env(tmp_path) as env:
        yield Run(env, Provider(env))
        for workspaces in env.hosts:
            asyncio.run(workspaces.close())


def status(result: ExecutionResult) -> ObservationStatus:
    return result.observation.observation.status


@pytest.mark.asyncio
async def test_a_turn_dispatched_through_the_builder_has_its_write_reverted(run: Run) -> None:
    before = digest(run.path)
    await run.execute(ensure_request())
    result = await run.execute(run.dispatch())
    assert status(result) is not ObservationStatus.SUCCEEDED  # the violation is reported
    assert digest(run.path) == before
    assert run.provider.turns


@pytest.mark.asyncio
async def test_the_builders_lifecycle_executor_settles_what_a_dead_provider_wrote(
    run: Run,
) -> None:
    before = digest(run.path)
    run.provider.dies = True
    await run.execute(ensure_request())
    lost = await run.execute(run.dispatch())
    assert status(lost) is ObservationStatus.UNKNOWN
    assert digest(run.path) != before  # written and not yet judged
    cancelled = await run.execute(cancel_request())
    assert status(cancelled) is ObservationStatus.CANCELLED
    assert digest(run.path) == before


@pytest.mark.asyncio
async def test_a_snapshot_right_after_a_restart_is_refused_until_settlement_reverts_the_write(
    run: Run,
) -> None:
    before = digest(run.path)
    run.provider.dies = True
    await run.execute(ensure_request())
    await run.execute(run.dispatch())
    assert digest(run.path) != before

    run.start()  # a fresh host over the same Project; no executor has run a request yet
    written = digest(run.path)
    with pytest.raises(RuntimeContractError, match="fenced"):
        await run.env.hosts[-1].root.snapshot("early")
    assert digest(run.path) == written  # refused without adopting the write

    run.provider.dies = False
    cancelled = await run.execute(cancel_request())
    assert status(cancelled) is ObservationStatus.CANCELLED
    assert digest(run.path) == before
    assert await run.env.hosts[-1].root.snapshot("after")
