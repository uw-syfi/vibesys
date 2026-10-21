"""Every member of each matching event vocabulary exists in native records."""

from datetime import UTC, datetime
from enum import Enum
from types import UnionType
from typing import Literal, Union, get_args, get_origin

import pytest
from pydantic import BaseModel, ValidationError

import vibesys.events as e
import vs_proto.api as p
from vibesys.api import SteerDelivery, SteerTarget
from vibesys.records.api import MappingState, map_event
from vibesys.search_spans.api import FailedCheck
from vs_core.api import EvidenceKind, Span, SpanAttempt, SpanFailure, WaitingOn


def vocabulary(annotation: object) -> tuple[str, ...]:
    if annotation is type(None):
        return ()
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return tuple(str(member.value) for member in annotation)
    if get_origin(annotation) is Literal:
        return tuple(str(value) for value in get_args(annotation))
    if get_origin(annotation) in {Union, UnionType}:
        return tuple(value for part in get_args(annotation) for value in vocabulary(part))
    message = f"expected a closed vocabulary, got {annotation!r}"
    raise TypeError(message)


@pytest.mark.parametrize(
    ("source", "field", "target"),
    [
        (e.RunStartupProgressData, "stage", p.StartupStage),
        (e.RunStartupProgressData, "state", p.StageState),
        (e.RunStartupProgressData, "condition", p.WaitCondition),
        (e.RunEndingData, "reason", p.EndReason),
        (e.WorkspaceRootProbeData, "state", p.ProbeState),
        (e.QuotaPausedData, "condition", p.CapacityCondition),
        (e.QuotaPausedData, "policy", p.CapacityPolicy),
        (e.ProviderSwitchedData, "reason", p.ProviderSwitchReason),
        (SteerDelivery, "action", p.IntentAction),
        (SteerDelivery, "mode", p.SteerMode),
        (SteerTarget, "kind", p.SteerTargetKind),
        pytest.param(FailedCheck, "check", p.EvidenceKind, marks=pytest.mark.regression),
        (Span, "type", p.RuntimeType),
        (Span, "status", p.SpanStatus),
        (WaitingOn, "kind", p.WaitKind),
        (SpanAttempt, "outcome", p.AttemptOutcome),
        (SpanFailure, "kind", p.SpanFailureKind),
        (e.WorkstreamFailureData, "kind", p.WorkstreamFailureKind),
        (e.WinnerSelectedData, "selection", p.Selection),
        (e.GateStartedData, "gate", p.GateStage),
        (e.GateStartedData, "source", p.FrameworkSource),
        (e.InputMeasurementFailedData, "failure", p.InputFailureKind),
        (e.MeasurementComparedData, "direction", p.Direction),
        (e.MeasurementComparedData, "verdict", p.ComparisonVerdict),
        (e.ComparisonReadingData, "status", p.BaselineStatus),
    ],
)
def test_matching_event_to_record_vocabularies_are_total(
    source: type[BaseModel], field: str, target: type[Enum]
) -> None:
    values = vocabulary(source.model_fields[field].annotation)
    assert values
    for value in values:
        assert target[value.upper().replace("-", "_")].name != "UNSPECIFIED"


@pytest.mark.regression
def test_profiling_is_not_a_pass_fail_check() -> None:
    with pytest.raises(ValidationError, match="check"):
        FailedCheck.model_validate({"check": EvidenceKind.PROFILING})


@pytest.mark.parametrize(
    "channel", vocabulary(e.AgentOutputChunkData.model_fields["channel"].annotation)
)
def test_every_output_channel_has_a_native_disposition(channel: str) -> None:
    result = map_event(
        MappingState("run-a"),
        e.CoreEvent(
            type=e.CoreEventType.AGENT_OUTPUT_CHUNK,
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            data=e.AgentOutputChunkData.model_validate({"channel": channel, "content": "output"}),
        ),
    )
    assert bool(result.streams) is (channel != "tool")
