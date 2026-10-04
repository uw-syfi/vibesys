"""Synthesized evidence reads preserve coverage and bound serialized replies."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.support.evaluation_scenarios import ScenarioSpec, build_scenario

from vs_evaluation.api import (
    ContentDigest,
    EvaluationAgentRole,
    EvaluationAgentService,
    EvidenceArgs,
    EvidenceFingerprints,
    EvidenceKind,
    EvidenceMetric,
    EvidenceOutcome,
    EvidencePageError,
    EvidencePages,
    PartialMeasurement,
    TrustedEvidence,
)
from vs_evaluation.api.testing import InMemoryEvaluationNamespace
from vs_evaluation.api.tools import build_evaluation_tools
from vs_runtime.api import AgentRole, AgentToolBindingContext

if TYPE_CHECKING:
    from pathlib import Path


@st.composite
def evidence_sets(draw: st.DrawFn) -> tuple[TrustedEvidence, ...]:
    """Generate complete trusted records, including hostile narrative and metrics."""
    size = draw(st.integers(min_value=0, max_value=45))
    metric_name = draw(st.text(min_size=1, max_size=5500))
    outcomes = draw(st.lists(st.sampled_from(tuple(EvidenceOutcome)), min_size=size, max_size=size))
    kinds = draw(st.lists(st.sampled_from(tuple(EvidenceKind)), min_size=size, max_size=size))
    partial = draw(st.booleans())
    digest = ContentDigest(value="a" * 64)
    fingerprints = EvidenceFingerprints(
        candidate=digest, evaluator=digest, workload=digest, environment=digest
    )
    return tuple(
        TrustedEvidence(
            evidence_id=f"{index:064x}",
            evaluation_id=f"evaluation-{index}",
            stage_name=kinds[index].value,
            kind=kinds[index],
            fingerprints=fingerprints,
            trusted_inputs=digest,
            outcome=outcomes[index],
            semantic_summary="large summary " * 1000,
            metrics=(EvidenceMetric(name=metric_name, value=float(index)),),
            accepted_round=index,
            partial_measurement=PartialMeasurement(name="warmup", value=1, direction="max")
            if partial
            else None,
        )
        for index in range(size)
    )


@given(records=evidence_sets(), matched=st.booleans())
def test_pages_bound_cover_and_return_each_reference_once(
    records: tuple[TrustedEvidence, ...], *, matched: bool
) -> None:
    pages = EvidencePages()
    args = EvidenceArgs(workload=("a" if matched else "b") * 64)
    ids: list[str] = []
    while True:
        reply = pages.query(records, args, capability="reader")
        document = json.loads(reply.model_dump_json())
        assert len(reply.model_dump_json()) <= 4000
        assert reply.coverage is not None
        assert sum(reply.coverage.by_kind.values()) == len(records)
        assert sum(reply.coverage.by_outcome.values()) == len(records)
        for status in ("question_coverage", "unsupported", "inconclusive", "workload_mismatch"):
            assert status in document["coverage"]
        for row in document["evidence"]:
            original = next(
                record for record in records if record.evidence_id == row["evidence_id"]
            )
            assert len(row["metrics"]) + row["metrics_omitted"] == len(original.metrics)
            assert row["partial_measurement_available"] == (
                original.partial_measurement is not None
            )
            assert row["workload_mismatch"] == ("matched" if matched else "mismatched")
        ids.extend(row.evidence_id for row in reply.evidence)
        assert reply.omitted == len(records) - len(ids)
        if reply.next_cursor is None:
            break
        assert reply.evidence
        args = EvidenceArgs(cursor=reply.next_cursor)
        records_for_continuation = ()
        reply_after_change = pages.query(records_for_continuation, args, capability="reader")
        assert reply_after_change.coverage == reply.coverage
    assert sorted(ids) == sorted(record.evidence_id for record in records)
    assert len(ids) == len(set(ids))
    for record in records:
        detail = pages.query(
            records, EvidenceArgs(reference_id=record.evidence_id), capability="reader"
        )
        assert detail.evidence == (record,)
    assert pages.query(records, EvidenceArgs(full=True), capability="reader").evidence == tuple(
        sorted(records, key=lambda item: (-item.accepted_round, item.evidence_id))
    )


@given(records=evidence_sets())
def test_cursor_capability_isolation(records: tuple[TrustedEvidence, ...]) -> None:
    pages = EvidencePages()
    reply = pages.query(records, EvidenceArgs(), capability="first")
    if reply.next_cursor is not None:
        with pytest.raises(EvidencePageError, match="cursor"):
            pages.query(records, EvidenceArgs(cursor=reply.next_cursor), capability="second")
    with pytest.raises(EvidencePageError, match="cursor"):
        EvidencePages().query(records, EvidenceArgs(cursor="f" * 64), capability="first")
    with pytest.raises(EvidencePageError, match="reference_id"):
        pages.query(records, EvidenceArgs(reference_id="f" * 64), capability="first")


@pytest.mark.parametrize(
    "arguments",
    [
        {"cursor": "a" * 64, "full": True},
        {"reference_id": "a" * 64, "full": True},
        {"cursor": "a" * 64, "workload": "a" * 64},
        {"cursor": "a" * 64, "evidence_kinds": ["profile"]},
    ],
)
def test_conflicting_read_modes_fail_at_boundary(arguments: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        EvidenceArgs.model_validate(arguments)


def test_evicted_cursor_fails_explicitly() -> None:
    digest = ContentDigest(value="a" * 64)
    fingerprints = EvidenceFingerprints(
        candidate=digest, evaluator=digest, workload=digest, environment=digest
    )
    records = tuple(
        TrustedEvidence(
            evidence_id=f"{index:064x}",
            evaluation_id=str(index),
            stage_name="accuracy",
            kind=EvidenceKind.ACCURACY,
            outcome=EvidenceOutcome.PASSED,
            fingerprints=fingerprints,
            trusted_inputs=digest,
            accepted_round=index,
        )
        for index in range(30)
    )
    pages = EvidencePages()
    first = pages.query(records, EvidenceArgs(), capability="first")
    assert first.next_cursor is not None
    for index in range(128):
        pages.query(records, EvidenceArgs(), capability=f"other-{index}")
    with pytest.raises(EvidencePageError, match="expired"):
        pages.query(records, EvidenceArgs(cursor=first.next_cursor), capability="first")


@pytest.mark.asyncio
async def test_actual_mcp_evidence_read_has_bounded_default_and_explicit_detail(
    tmp_path: Path,
) -> None:
    async with build_scenario(tmp_path / "scenario", ScenarioSpec()) as scenario:
        scenario.backend.bind(
            AgentToolBindingContext(
                AgentRole(id="judge", system_prompt="test"),
                scenario.workspaces_impl.root,
                None,
                str,
            )
        )
        service = EvaluationAgentService(
            scenario.backend, InMemoryEvaluationNamespace(), tmp_path / "evidence.sock"
        )
        grant = service.grant(principal_id="reader", role=EvaluationAgentRole.JUDGE, scope_id=None)
        await service.start()
        try:
            tool = next(
                tool
                for tool in build_evaluation_tools(
                    socket_path=service.socket_path, token=grant.token, role=grant.role
                )
                if tool.name == "accepted_evidence"
            )
            overview = await asyncio.to_thread(tool.handler, EvidenceArgs())
            assert len(overview) <= 4000
            document = json.loads(overview)
            assert document["evidence"]
            assert document["coverage"]["question_coverage"] == "unknown"
            for row in document["evidence"]:
                detail = json.loads(
                    await asyncio.to_thread(
                        tool.handler, EvidenceArgs(reference_id=row["evidence_id"])
                    )
                )
                assert "fingerprints" in detail["evidence"][0]
        finally:
            await service.close()
