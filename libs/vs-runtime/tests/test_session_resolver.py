"""The production session resolver over real Git, a real artifact store and the Fake agent client."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.executor_context import context_for
from tests.support.session_world import (
    ROLE,
    SCHEMA,
    SCOPE,
    Reply,
    dispatch_request,
    ensure_request,
    turn_spec,
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
from vs_core.api import (
    Access,
    ArtifactId,
    ArtifactRef,
    DispatchTurn,
    InputId,
    ObservationStatus,
    RequestId,
    ScopeInputTarget,
    SessionInput,
)
from vs_runtime.api import AgentRole, ArtifactStore, WorkspaceAccess
from vs_runtime.api.core import (
    ExecutionResult,
    ProductionSessionResolver,
    ReceiptStore,
    ResolverInputs,
    StoreWorkspaceReceipts,
    open_session_requests,
)
from vs_runtime.api.infrastructure import AgentExecutionConfiguration

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from vs_core.api import RequestBase
    from vs_runtime.api.infrastructure import RuntimeWorkspace


@pytest.fixture(autouse=True)
def isolated_project_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "operator-state"))


def role(access: WorkspaceAccess = WorkspaceAccess.READ_ONLY) -> AgentRole:
    return AgentRole(id=ROLE.root, system_prompt="You are a worker.", workspace_access=access)


@dataclass
class Production:
    """A run's real pieces with the production resolver in front of the Fake agent."""

    env: WorkspaceEnv
    artifacts: ArtifactStore
    resolver: ProductionSessionResolver
    store: ReceiptStore
    requests: Any
    turns: list[AgentTurnRequest]

    @property
    def root(self) -> RuntimeWorkspace:
        return self.env.hosts[-1].root

    def put(self, text: str) -> ArtifactRef:
        """Store *text* and return the reference whose digest names it."""
        receipt = self.artifacts.write(text.encode())
        return ArtifactRef(
            artifact_id=ArtifactId(root=f"artifact-{receipt.sha256[:8]}"), digest=receipt.sha256
        )

    async def execute(self, request: RequestBase) -> ExecutionResult:
        outcome = await self.requests.execute(cast("Any", request), context_for(request))
        assert isinstance(outcome, ExecutionResult), outcome
        return outcome


def open_production(env: WorkspaceEnv, declared: AgentRole) -> Production:
    store = ReceiptStore(env.receipts_namespace())
    artifacts = ArtifactStore(env.project.state.local_namespace(RUN_ID, "artifacts"))
    turns: list[AgentTurnRequest] = []

    def spec(selected: AgentRole, path: Path) -> AgentSessionSpec:
        return AgentSessionSpec(
            role=selected.id,
            provider="fake",
            workspace=path,
            policy=AgentExecutionPolicy(require_enforcement=False),
        )

    inputs = ResolverInputs(
        roles=(declared,),
        schemas={SCHEMA: Reply},
        workspaces=env.hosts[-1],
        workspace_receipts=StoreWorkspaceReceipts(store),
        artifacts=artifacts,
        configuration=lambda selected: AgentExecutionConfiguration(
            selected.id, AgentSpec(backend=AgentBackend.STUB, cli_timeout=45)
        ),
        session_spec=spec,
    )
    client = AgentClient(FakeDriver(answer={"value": 7}, on_turn=turns.append))
    requests = open_session_requests(
        inputs, client=client, invocation_slot=FakeAgentInvocationStore(), store=store
    ).turns
    return Production(env, artifacts, ProductionSessionResolver(inputs), store, requests, turns)


@pytest.fixture
def production(tmp_path: Path) -> Iterator[Production]:
    with open_workspace_env(tmp_path) as env:
        yield open_production(env, role())
        for workspaces in env.hosts:
            asyncio.run(workspaces.close())


def turn_with(prompts: tuple[ArtifactRef, ...], invocation: str = "inv-1") -> DispatchTurn:
    request = dispatch_request(invocation=invocation)
    return request.model_copy(
        update={"turn": turn_spec(invocation).model_copy(update={"prompts": prompts})}
    )


def reserved(artifact: ArtifactRef, number: int) -> SessionInput:
    return SessionInput(
        input_id=InputId(root=f"input-{number}"),
        target=ScopeInputTarget(scope=SCOPE),
        artifact=artifact,
        received_at=1.0,
        sequence=number,
    )


@pytest.mark.asyncio
async def test_a_turn_runs_with_the_role_instructions_timeout_and_rendered_artifacts(
    production: Production,
) -> None:
    prompt = production.put("Optimise the kernel.")
    await production.execute(ensure_request())
    result = await production.execute(turn_with((prompt,)))
    assert result.observation.observation.status is ObservationStatus.SUCCEEDED
    (turn,) = production.turns
    assert turn.instructions == "You are a worker."
    assert turn.timeout is not None
    assert turn.timeout.total_seconds() == 45
    assert "Optimise the kernel." in turn.message


@settings(
    max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    prompts=st.lists(st.text(alphabet="abcdef", min_size=1, max_size=12), min_size=1, max_size=3),
    notes=st.lists(st.text(alphabet="uvwxyz", min_size=1, max_size=8), max_size=3, unique=True),
)
def test_the_message_carries_every_prompt_then_the_inputs_in_sequence_order(
    tmp_path_factory: pytest.TempPathFactory, prompts: list[str], notes: list[str]
) -> None:
    with open_workspace_env(tmp_path_factory.mktemp("resolver")) as env:
        production = open_production(env, role())
        refs = tuple(production.put(f"P{index}:{text}") for index, text in enumerate(prompts))
        inputs = tuple(
            reserved(production.put(f"N{index}:{note}"), index) for index, note in enumerate(notes)
        )
        message = production.resolver.message(
            turn_spec().model_copy(update={"prompts": refs}), tuple(reversed(inputs))
        )
        assert message is not None
        positions = [message.index(f"P{index}:{text}") for index, text in enumerate(prompts)]
        positions += [message.index(f"N{index}:{note}") for index, note in enumerate(notes)]
        assert positions == sorted(positions)
        assert message == production.resolver.message(
            turn_spec().model_copy(update={"prompts": refs}), inputs
        )
        for workspaces in env.hosts:
            asyncio.run(workspaces.close())


@pytest.mark.asyncio
async def test_a_missing_or_tampered_artifact_rejects_the_turn_before_any_provider_call(
    production: Production,
) -> None:
    await production.execute(ensure_request())
    stranger = ArtifactRef(
        artifact_id=ArtifactId(root="gone"), digest=hashlib.sha256(b"never stored").hexdigest()
    )
    missing = await production.execute(turn_with((stranger,)))
    assert missing.observation.observation.status is ObservationStatus.REJECTED

    stored = production.put("original")
    path = production.artifacts._root / stored.digest  # noqa: SLF001  # lint-waiver: LW-837233 [SLF001]; tampering with the stored object is the point of this test.
    path.chmod(0o644)
    path.write_bytes(b"tampered")
    tampered = await production.execute(
        turn_with((stored,), "inv-2").model_copy(update={"request_id": RequestId(root="req-2")})
    )
    assert tampered.observation.observation.status is ObservationStatus.REJECTED
    assert production.turns == []


@pytest.mark.parametrize(
    ("requested", "declared", "granted"),
    [
        (Access.READ_ONLY, WorkspaceAccess.READ_WRITE, WorkspaceAccess.READ_ONLY),
        (Access.WRITE_CANDIDATE, WorkspaceAccess.READ_WRITE, WorkspaceAccess.READ_WRITE),
        (Access.WRITE_CANDIDATE, WorkspaceAccess.READ_ONLY, WorkspaceAccess.READ_ONLY),
        (Access.WRITE_CANDIDATE, WorkspaceAccess.LIMITED, WorkspaceAccess.LIMITED),
        (Access.WRITE_ARTIFACTS, WorkspaceAccess.READ_WRITE, WorkspaceAccess.LIMITED),
        (Access.WRITE_ARTIFACTS, WorkspaceAccess.READ_ONLY, WorkspaceAccess.READ_ONLY),
    ],
)
def test_a_grant_never_exceeds_the_role_declaration(
    tmp_path: Path, requested: Access, declared: WorkspaceAccess, granted: WorkspaceAccess
) -> None:
    with open_workspace_env(tmp_path) as env:
        production = open_production(env, role(declared))
        turn = turn_spec().model_copy(
            update={"session": turn_spec().session.model_copy(update={"access": requested})}
        )
        grant = production.resolver.access_grant(turn)
        assert grant is not None
        assert grant.access is granted
        for workspaces in env.hosts:
            asyncio.run(workspaces.close())


@pytest.mark.asyncio
async def test_an_undeclared_role_is_unknown_to_the_resolver(production: Production) -> None:
    assert production.resolver.knows_role(ROLE)
    assert not production.resolver.knows_role(ROLE.model_copy(update={"root": "stranger"}))
    assert production.resolver.turn_timeout(ROLE.model_copy(update={"root": "stranger"})) is None
