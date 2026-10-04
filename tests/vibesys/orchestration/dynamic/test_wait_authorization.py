"""Principal wait authority and typed correction through public host interfaces."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import RootModel
from tests.vibesys.orchestration.dynamic._support import Script, baseline_run

from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, PROFILER
from vibesys.orchestration.dynamic.models import ImplementerReply
from vibesys.orchestration.structured_turn import structured_turn
from vs_evaluation.api import (
    AccessErrorCode,
    ContentDigest,
    EvaluationAgentAccessError,
    EvaluationAgentRole,
    EvaluationAgentService,
    EvaluationRequest,
    EvaluationStep,
    EvidenceFingerprints,
    evaluation_principal,
)
from vs_evaluation.api.testing import FakeEvaluationSettlements
from vs_runtime.api.testing import FakeEvaluation

if TYPE_CHECKING:
    from vs_runtime.api import AgentRole

ROLES = (IMPLEMENTER, JUDGE, PROFILER)
KINDS = ("own", "foreign", "own_profiler", "foreign_profiler", "unknown", "malformed")


@pytest.mark.parametrize("role", ROLES, ids=lambda role: role.id)
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("implementation", ["service", "fake"])
def test_wait_contract_checks_kind_and_principal(
    role: AgentRole,
    kind: Literal["own", "foreign", "own_profiler", "foreign_profiler", "unknown", "malformed"],
    implementation: Literal["service", "fake"],
) -> None:
    """Every role obeys the same whole-class authority contract in both implementations."""

    async def scenario() -> None:
        settlements = FakeEvaluationSettlements()
        service = EvaluationAgentService(
            settlements.backend, settlements.namespace, Path("unused.sock")
        )
        evaluation = FakeEvaluation(settlement_observations=settlements)
        grant_role = EvaluationAgentRole(role.id.removeprefix("dynamic-"))
        principal = evaluation_principal(grant_role, "caller", "scope")
        digest = ContentDigest.sha256(b"snapshot")
        fingerprints = EvidenceFingerprints(
            candidate=digest, evaluator=digest, workload=digest, environment=digest
        )
        own = await settlements.submit(
            EvaluationRequest(
                key="own",
                owner_scope="scope",
                stages=(EvaluationStep(name="benchmark", payload={}),),
            ),
            fingerprints,
            principal_id=principal,
        )
        foreign = await settlements.submit(
            EvaluationRequest(
                key="foreign",
                owner_scope="scope",
                stages=(EvaluationStep(name="benchmark", payload={}),),
            ),
            fingerprints,
            principal_id="implementer:other",
        )
        handle = {
            "own": own,
            "foreign": foreign,
            "own_profiler": "a" * 32,
            "foreign_profiler": "b" * 32,
            "unknown": "eval_unknown",
            "malformed": " bad ",
        }[kind]
        validate = (
            service.validate_wait if implementation == "service" else evaluation.validate_wait
        )
        if kind == "own":
            await validate((handle,), scope_id="scope", principal_id=principal)
        else:
            with pytest.raises(EvaluationAgentAccessError) as caught:
                await validate((handle,), scope_id="scope", principal_id=principal)
            assert caught.value.code in {
                AccessErrorCode.HANDLE_DENIED,
                AccessErrorCode.UNKNOWN_HANDLE,
            }
        # Invalid validation is observational: both pending captures retain ownership.
        assert len(await settlements.submission_history("scope")) == 2

    asyncio.run(scenario())


@given(st.text())
def test_arbitrary_unregistered_handles_are_typed_errors(handle: str) -> None:
    async def scenario() -> None:
        settlements = FakeEvaluationSettlements()
        service = EvaluationAgentService(
            settlements.backend, settlements.namespace, Path("unused.sock")
        )
        with pytest.raises(EvaluationAgentAccessError):
            await service.validate_wait(
                (handle,), scope_id="scope", principal_id="implementer:caller"
            )

    asyncio.run(scenario())


def test_invalid_wait_is_corrected_in_the_same_conversation(tmp_path: Path) -> None:
    """A non-evaluation final handle resumes the completed turn instead of escaping."""

    async def scenario() -> None:
        replies = iter(
            (
                {"kind": "waiting_for_evaluation", "handles": ["profiler-operation"]},
                {"summary": "Pending profile does not justify waiting.", "outcome": "continue"},
            )
        )
        run = baseline_run(tmp_path, Script({}), responder=lambda *_args: next(replies))
        workspace = await run.workspaces.create_candidate(member_id="caller")
        session = await run.agents.create_session(
            IMPLEMENTER, workspace=workspace, member_id="caller"
        )
        settlements = FakeEvaluationSettlements()
        service = EvaluationAgentService(
            settlements.backend, settlements.namespace, Path("unused.sock")
        )

        async def validate(reply: RootModel[ImplementerReply]) -> None:
            if hasattr(reply.root, "handles"):
                await service.validate_wait(
                    reply.root.handles, scope_id=workspace.id, principal_id="implementer:caller"
                )

        result = await structured_turn(
            session,
            "Evaluate the candidate.",
            RootModel[ImplementerReply],
            validate_response=validate,
        )
        assert result.root.summary == "Pending profile does not justify waiting."
        assert len(session.history) == 2

    asyncio.run(scenario())
