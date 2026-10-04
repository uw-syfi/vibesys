"""Implementation evidence preserves immutable capture identity after later edits."""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Literal

from hypothesis import example, given
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._support import dynamic_options, portfolio

from vibesys.orchestration.dynamic import PLUGIN, DynamicState
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vs_runtime.api import AgentCapability
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_runtime.api import AgentRole


@given(
    suffix=st.binary(min_size=1, max_size=16),
    explicit=st.booleans(),
    mismatched=st.booleans(),
    reference_kind=st.sampled_from(("evaluation", "evidence", "profiler")),
)
@example(suffix=b"\x18\xfb", reference_kind="evaluation", explicit=False, mismatched=False)
def test_measurement_reference_keeps_measured_revision_after_implementation(
    suffix: bytes,
    reference_kind: Literal["evaluation", "evidence", "profiler"],
    *,
    explicit: bool,
    mismatched: bool,
) -> None:
    """Every measurement reference remains tied to its capture, not the final snapshot."""
    handle = f"eval_{suffix.hex()}"
    measured = f"measured-{suffix.hex()}"
    location = {
        "evaluation": handle,
        "evidence": suffix.hex().ljust(64, "0"),
        "profiler": f"profiler-{suffix.hex()}",
    }[reference_kind]

    async def scenario(project_root: Path) -> None:
        implementer_turns = 0

        def respond(
            role: AgentRole,
            _history: tuple[str, ...],
            _message: str,
            _response: type[BaseModel] | None,
        ) -> object:
            nonlocal implementer_turns
            if role.id == ORCHESTRATOR.id:
                return portfolio("capture")
            if role.id == JUDGE.id:
                return {"passed": True, "analysis": "Evidence is correctly attributed."}
            assert role.id == IMPLEMENTER.id
            implementer_turns += 1
            run.evaluation.submitted_revisions[handle] = measured
            if reference_kind == "evidence":
                run.evaluation.accepted_evidence[handle] = (location,)
            elif reference_kind == "profiler":
                run.evaluation.profiler_revisions[location] = measured
            reference: dict[str, object] = {"location": location, "purpose": "measured throughput"}
            if mismatched and implementer_turns == 1:
                reference["revision"] = "different-revision"
            elif explicit:
                reference["revision"] = measured
            return {
                "summary": "Measured, then continued editing.",
                "outcome": "nominated",
                "evidence": [
                    reference,
                    {"location": "report/local.json", "purpose": "final local checks"},
                    {
                        "location": "report/prior.json",
                        "purpose": "earlier local checks",
                        "revision": "prior-local-revision",
                    },
                ],
            }

        run = FakeRun(
            PLUGIN,
            project_root=project_root,
            responder=respond,
            supports_parallel_candidates=True,
            supported_extra_tools={"evaluation", "profiler"},
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
                AgentCapability.DURABLE_TURN_CONTINUATION,
            },
        )
        await PLUGIN.orchestrate(run, dynamic_options(judge_every=1, max_in_flight=1))
        state = await run.state.load(DynamicState)
        assert state is not None
        item = state.workstreams[0]
        assert item.implementation is not None
        evaluation, local, historic = item.implementation.evidence
        assert evaluation.revision == measured
        assert evaluation.revision != item.candidate_revision
        assert local.revision == item.candidate_revision
        assert historic.revision == "prior-local-revision"
        assert implementer_turns == (2 if mismatched else 1)
        assert item.budget.spent == 1

    with TemporaryDirectory(prefix="loopfix-evidence-") as directory:
        asyncio.run(scenario(Path(directory)))
