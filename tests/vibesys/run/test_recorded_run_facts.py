"""The run records its project root and the facts the screen shows."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import vibesys.events as e
import vs_proto.api as p
from server.run_usage import UsageFold
from vibesys.records.api import MappingState, map_event
from vibesys.run import EventJournal
from vs_core.api import CheckoutStatus
from vs_project.api import Project
from vs_runlog.api import RunLogReader
from vs_sim.api.testing import ManualClock, SimThreads

_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _event(
    kind: e.CoreEventType, data: e.CoreEventData | None = None, **fields: object
) -> e.CoreEvent:
    return e.CoreEvent.model_validate(
        {"type": kind, "data": data, "timestamp": _AT, "run_id": "run-a", "sequence": 1, **fields}
    )


@pytest.mark.regression
def test_epoch_records_the_canonical_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    root.mkdir()
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    project = Project.open(link)
    clock = ManualClock(start=1)
    journal = EventJournal(clock, SimThreads())
    journal.checkout = ("abc", False)
    journal.enable_runlog(project, "run-a")
    journal.attach(Project.log_directory_for(project.root, "run-a"), "run-a")
    journal.emit(
        e.CoreEventType.RUN_STARTED,
        data=e.RunStartedData(outer_loop="plain", input="input"),
    )
    journal.close()
    with Project.run_log_path_for(project.root, "run-a").open("rb") as stream:
        records = list(RunLogReader(stream))
    headers = [
        record.body.value
        for record in records
        if record.body is not None and record.body.field == "epoch_started"
    ]
    assert headers
    assert headers[0].project_root == str(project.root)
    assert project.root == root.resolve()


@pytest.mark.regression
def test_adoption_records_the_checkout_skip() -> None:
    started = e.SpanStartedData.model_validate(
        {
            "span": {
                "id": "op:adopt",
                "parent": None,
                "type": "operation",
                "name": "adopt",
                "status": "running",
                "started_at": 0,
                "label": {
                    "kind": "adoption",
                    "attrs_json": json.dumps({}),
                    "reason": CheckoutStatus.OPERATOR_CHANGED_CHECKOUT.value,
                },
            }
        }
    )
    state = map_event(MappingState("run-a"), _event(e.CoreEventType.SPAN_STARTED, started)).state
    result = map_event(
        state,
        _event(
            e.CoreEventType.WINNER_SELECTED,
            e.WinnerSelectedData(selection="baseline", revision="input"),
        ),
    )
    spans = [
        record.body.value.span
        for record in result.records
        if record.body is not None
        and record.body.field == "span"
        and record.body.value.span is not None
        and record.body.value.span.label is not None
        and record.body.value.span.label.kind == "adoption"
    ]
    assert spans
    label = spans[-1].label
    assert label is not None
    assert label.attrs is not None
    attrs = p.AdoptionAttrs.from_binary(label.attrs.value)
    assert attrs.checkout_status == p.CheckoutStatus.OPERATOR_CHANGED_CHECKOUT


@pytest.mark.regression
def test_scripted_turn_keeps_the_selected_provider_and_model() -> None:
    fold = UsageFold()
    fold.consume(
        {
            "type": "agent_execution_started",
            "execution_id": "exec-1",
            "agent_kind": "implementer",
            "data": {
                "kind": "agent_execution_started",
                "client": "scripted",
                "provider": "claude",
                "model": "configured-model",
            },
        }
    )
    record = fold.consume(
        {
            "type": "turn_usage_recorded",
            "execution_id": "exec-1",
            "run_id": "run-a",
            "data": {"kind": "turn_usage_recorded", "span_id": "turn-1"},
        }
    )
    assert record is not None
    assert record.client == "scripted"
    assert record.harness == "claude"
    assert record.model == "configured-model"
    result = map_event(
        MappingState("run-a"),
        _event(
            e.CoreEventType.AGENT_EXECUTION_STARTED,
            e.AgentExecutionStartedData(
                stage="implementer",
                client="scripted",
                provider="claude",
                model="configured-model",
                activity=e.AgentExecutionActivityData(mode="thinking", summary="working"),
            ),
            execution_id="exec-1",
        ),
    )
    turns = [
        record.body.value.span
        for record in result.records
        if record.body is not None
        and record.body.field == "span"
        and record.body.value.span is not None
        and record.body.value.span.label is not None
        and record.body.value.span.label.kind == "agent_turn"
    ]
    assert turns
    label = turns[-1].label
    assert label is not None
    assert label.attrs is not None
    attrs = p.AgentTurnAttrs.from_binary(label.attrs.value)
    assert attrs.client == p.AgentClient.SCRIPTED
    assert attrs.model == "configured-model"
    assert attrs.provider == "claude"
    turn = {
        "span": {
            "id": "op:exec-1",
            "parent": None,
            "type": "operation",
            "name": "implementer",
            "status": "running",
            "started_at": 0,
            "invocation": "exec-1",
            "label": {
                "kind": "agent_turn",
                "attrs_json": json.dumps(
                    {
                        "agent": "implementer",
                        "role": "implementer",
                        "model": "configured-model",
                    }
                ),
            },
        }
    }
    state = map_event(
        MappingState("run-a"),
        _event(e.CoreEventType.SPAN_STARTED, e.SpanStartedData.model_validate(turn)),
    ).state
    state = map_event(
        state,
        _event(
            e.CoreEventType.AGENT_EXECUTION_STARTED,
            e.AgentExecutionStartedData(
                stage="implementer",
                client="scripted",
                provider="claude",
                model="configured-model",
                activity=e.AgentExecutionActivityData(mode="thinking", summary="working"),
            ),
            execution_id="exec-1",
        ),
    ).state
    again = map_event(
        state, _event(e.CoreEventType.SPAN_UPDATED, e.SpanUpdatedData.model_validate(turn))
    )
    for record in again.records:
        p.validate(record)
    kept = [
        record.body.value.span
        for record in again.records
        if record.body is not None
        and record.body.field == "span"
        and record.body.value.span is not None
        and record.body.value.span.label is not None
        and record.body.value.span.label.kind == "agent_turn"
    ]
    assert kept
    kept_label = kept[-1].label
    assert kept_label is not None
    assert kept_label.attrs is not None
    kept_attrs = p.AgentTurnAttrs.from_binary(kept_label.attrs.value)
    assert kept_attrs.client == p.AgentClient.SCRIPTED
    assert kept_attrs.model == "configured-model"
    assert kept_attrs.provider == "claude"
