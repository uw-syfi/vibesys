"""A prompt that cites trusted evidence states what the framework measured.

Regression: a resumed implementer and the judges were told only opaque evidence ids, so a
trusted benchmark of 3001.7 calls/s was reported "inconclusive" and the judge never saw the
number. For any trusted outcome, the rendered prompt now carries the measured value, its unit,
its direction and the stage verdict, read from the evidence ledger.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.rendered_prompts import REVISION, PromptRender, trusted_record
from tests.vibesys.domain_sections import EMPTY_DOMAIN_SECTIONS

from vibesys.orchestration.evolve.core_policy.api import prompts as evolve_prompts
from vibesys.orchestration.evolve.search.api import JudgePrompt as EvolveJudgePrompt
from vibesys.orchestration.evolve.search.api import evolve
from vibesys.prompts import CORE_PROTOCOL_DIR
from vs_core.api import EvidenceRef, ResumeFacts
from vs_evaluation.api import EvaluationStageOutcome, EvidenceOutcome, TrustedEvidence
from vs_runtime.api import RunFacts
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel

_FACTS = RunFacts(agent_git_writable=True, domain_id="generic", objective="Make it faster.")
_RUN = {
    "facts": _FACTS,
    "objective": _FACTS.objective,
    "environment_notes": "",
    "agent_evaluation": True,
    "domain_sections": EMPTY_DOMAIN_SECTIONS,
}
_EVIDENCE_ID = "b" * 64

# The reply types of the strategies whose prompts cite evidence; their schema names differ.
_REPLIES = dict(evolve.reply_schemas)


def _record(
    value: float, unit: str, direction: Literal["max", "min"], *, passed: bool
) -> tuple[EvidenceRef, TrustedEvidence]:
    return trusted_record(value, unit, direction, passed=passed, evidence_id=_EVIDENCE_ID)


def _render(
    root: Path,
    roots: tuple[Path, ...],
    context: BaseModel,
    held: tuple[EvidenceRef, TrustedEvidence],
    variables: Mapping[str, object] | None = None,
) -> str:
    return PromptRender(roots, _REPLIES, variables).render(root, context, held=held)


def _cases() -> dict[str, Callable[..., str]]:
    def core(root: Path, ref: EvidenceRef, evidence: TrustedEvidence) -> str:
        context = ResumeFacts(
            agent="implementer", retained=REVISION, ended_by=None, evidence=(ref,)
        )
        return _render(root, (CORE_PROTOCOL_DIR,), context, (ref, evidence))

    def evolve_judge(root: Path, ref: EvidenceRef, evidence: TrustedEvidence) -> str:
        context = EvolveJudgePrompt(candidate=REVISION, cited_evidence=(ref,))
        return _render(
            root,
            (Path(evolve_prompts.__file__).parent, CORE_PROTOCOL_DIR),
            context,
            (ref, evidence),
            _RUN,
        )

    return {
        "single/multi/dynamic/evolve/issue_queue resume": core,
        "evolve judge": evolve_judge,
    }


@pytest.mark.parametrize("name", sorted(_cases()))
@settings(
    max_examples=examples(pr=10, nightly=15),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    value=st.floats(0.1, 1e6, allow_nan=False).map(lambda x: round(x, 1)),
    unit=st.sampled_from(["calls/s", "tokens/s", "ms", "req/min"]),
    direction=st.sampled_from(["max", "min"]),
    passed=st.booleans(),
)
def test_a_prompt_citing_trusted_evidence_states_value_unit_direction_and_verdict(
    name: str, value: float, unit: str, direction: Literal["max", "min"], *, passed: bool
) -> None:
    ref, evidence = _record(value, unit, direction, passed=passed)
    with tempfile.TemporaryDirectory() as raw:
        text = _cases()[name](Path(raw), ref, evidence)
    assert f"{value} {unit}" in text
    assert f"better: {direction}" in text
    assert ("PASS" if passed else "FAIL") in text


@settings(max_examples=examples(pr=10, nightly=30), deadline=None)
@given(
    value=st.floats(0.1, 1e6, allow_nan=False).map(lambda x: round(x, 1)),
    unit=st.sampled_from(["calls/s", "tokens/s", "ms"]),
    direction=st.sampled_from(["max", "min"]),
    passed=st.booleans(),
)
def test_the_tool_reply_stage_carries_the_recorded_measurement(
    value: float, unit: str, direction: Literal["max", "min"], *, passed: bool
) -> None:
    _, evidence = _record(value, unit, direction, passed=passed)
    stage = EvaluationStageOutcome.of(evidence)
    (metric,) = stage.metrics
    assert (metric.value, metric.unit, metric.direction) == (value, unit, direction)
    assert stage.outcome is (EvidenceOutcome.PASSED if passed else EvidenceOutcome.FAILED)
    assert stage.evidence_id == evidence.evidence_id
