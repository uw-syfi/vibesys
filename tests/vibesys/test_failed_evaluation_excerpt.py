"""A failed evaluation's excerpt ends with the stage's own verdict, whatever the service logged.

The shared server's log tail travels with a failed Slurm stage. A checker prints a short verdict;
the server may print tens of kilobytes. The agent reads only the end of the stage's feedback, so
the verdict must be there and the service log can only come before it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.prompts import CORE_PROTOCOL_DIR
from vs_evaluation.api import (
    ContentDigest,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvidenceFingerprints,
    EvidenceKind,
    SemanticEvaluationStage,
    TrustedEvidence,
)
from vs_evaluation.api.testing import wait_until_executor_started
from vs_prompts.api import TemplateRenderer
from vs_runtime.api.slurm_evaluation_stack import ScenarioCluster, build_stack
from vs_sim.api.testing import examples, yield_until

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio

_HANDLE = "verdict-1"
_TERMINAL = frozenset({EvaluationState.SUCCEEDED, EvaluationState.FAILED})
_EXCERPT = """\
{% from '_evaluations.j2' import trusted_evaluations %}
{{ trusted_evaluations(items) }}"""


async def _feedback(tmp_path: Path, verdict: str, service_log: str) -> str:
    cluster = ScenarioCluster()
    cluster.accuracy_exit = 1
    cluster.accuracy_output = verdict
    cluster.service_log_tail = service_log
    stack = await build_stack(tmp_path, cluster)
    digest = ContentDigest.sha256(b"same")
    request = EvaluationRequest(
        key="verdict",
        stages=(
            EvaluationStep(
                name=EvidenceKind.ACCURACY.value,
                payload=SemanticEvaluationStage(
                    snapshot=stack.snapshot,
                    kind=EvidenceKind.ACCURACY,
                    fingerprints=EvidenceFingerprints(
                        candidate=digest, evaluator=digest, workload=digest, environment=digest
                    ),
                ).model_dump(mode="json"),
            ),
        ),
    )
    await stack.executor.submit(request, handle_id=_HANDLE)
    await wait_until_executor_started(cluster.accepted, stack.executor, _HANDLE)
    observed = await yield_until(
        lambda: stack.executor.inspect(_HANDLE),
        lambda seen: seen is not None and seen.state in _TERMINAL,
        "a terminal observation",
    )
    assert observed is not None
    (step,) = observed.stage_results
    await stack.executor.close()
    evidence = TrustedEvidence.model_validate(step.result)
    return evidence.semantic_summary or ""


def _excerpt(feedback: str) -> str:
    item = {
        "evidence_id": "e1",
        "kind": "correctness",
        "revision": {"revision_id": {"root": "abc123"}},
        "passed": False,
        "status": "settled",
        "metrics": [],
        "partial": None,
        "feedback": feedback,
    }
    return str(TemplateRenderer(CORE_PROTOCOL_DIR).render_string(_EXCERPT, items=[item]))


_ROUTES = "INFO 10-17 route: GET /v1/models, POST /v1/chat/completions, POST /tokenize\n"


async def test_a_checker_verdict_survives_a_56_kb_server_log(tmp_path: Path) -> None:
    verdict = (
        "checking sample 0..99\n" * 150
    ) + "FAIL: sample 17 expected 'Paris'\nFAIL: 3 of 100\n"
    log = _ROUTES * (56_000 // len(_ROUTES))

    excerpt = _excerpt(await _feedback(tmp_path, verdict, log))

    assert "FAIL: 3 of 100" in excerpt
    assert "FAIL: sample 17 expected" in excerpt


@settings(max_examples=examples(pr=5, nightly=30), deadline=None)
@given(
    verdict=st.text(
        st.characters(whitelist_categories=("L", "N", "Zs")), min_size=1, max_size=4_000
    ),
    log_size=st.integers(0, 60_000),
)
async def test_the_excerpt_ends_with_the_stage_output_for_any_sizes(
    verdict: str, log_size: int, tmp_path_factory: pytest.TempPathFactory
) -> None:
    ending = "FAIL: the verdict line"
    log = (_ROUTES * (log_size // len(_ROUTES) + 1))[:log_size]
    excerpt = _excerpt(
        await _feedback(tmp_path_factory.mktemp("verdict"), f"{verdict}\n{ending}", log)
    )

    assert ending in excerpt
