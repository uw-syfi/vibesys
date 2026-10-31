"""Regenerate the store record corpus with ``uv run python -m scripts.generate_record_goldens``.

Examples exercise every body, lifecycle state and built-in attrs type through
vs-proto's public surface. The corpus is a wire compatibility contract. The descriptor-bearing epoch is
local output, generated in tests so independent schema changes never rewrite a
shared binary. Tests decode the committed Any attrs using that embedded schema.
"""

from pathlib import Path

from protobuf import DescFile, Message
from protobuf.wkt.google.protobuf.any_pb import Any
from protobuf.wkt.google.protobuf.descriptor_pb import FileDescriptorSet

import vs_proto.api as p


def packed(message: Message) -> Any:
    """Pack a descriptor-backed strategy extension without a core import."""
    return Any(
        type_url="type.googleapis.com/" + message.desc().type_name, value=message.to_binary()
    )


def transcript_examples() -> dict[str, Message]:
    """Every transcript entry variant and both content representations."""
    inline = p.TranscriptContent(value=p.Oneof("inline", "hello"))
    blob = p.TranscriptContent(
        value=p.Oneof("blob", p.BlobRef(sha256="a" * 64, size=65537, media_type="text/plain"))
    )
    return {
        "text": p.TranscriptEntry(
            entry=p.Oneof(
                "text", p.TranscriptText(channel=p.TranscriptChannel.ASSISTANT, content=inline)
            )
        ),
        "call": p.TranscriptEntry(
            entry=p.Oneof(
                "tool_call",
                p.TranscriptToolCall(tool="shell", call_id="call-a", arguments_json=inline),
            )
        ),
        "result": p.TranscriptEntry(
            entry=p.Oneof(
                "tool_result",
                p.TranscriptToolResult(
                    tool="shell",
                    call_id="call-a",
                    content=blob,
                    payload=p.Oneof(
                        "command",
                        p.TranscriptCommandResult(
                            stdout=inline, stderr=blob, exit_code=2, duration_seconds=1.5
                        ),
                    ),
                ),
            ),
            at=p.Timestamp(seconds=1_700_000_000, nanos=250_000_000),
        ),
        "result_json": p.TranscriptEntry(
            entry=p.Oneof(
                "tool_result",
                p.TranscriptToolResult(
                    tool="search",
                    content=inline,
                    payload=p.Oneof("json", p.TranscriptContent(value=p.Oneof("inline", "[1]"))),
                ),
            )
        ),
    }


def detail_examples() -> dict[str, Message]:
    """Search run, ending and failure details carried by core Any fields."""
    return {
        "run_description": p.SearchRunDescription(max_workstreams=4),
        "ended": p.SearchEnded(reason=p.EndReason.NO_WINNER, uncompared=True),
        "run_failure": p.SearchRunFailure(
            workstreams_started=4,
            workstream_budget=4,
            candidates_kept=0,
            adopted_candidate="revision-b",
        ),
    }


def attrs_examples() -> dict[str, Message]:
    """Return deterministic, populated examples of each built-in span kind."""
    return {
        "agent_turn": p.AgentTurnAttrs(
            agent="agent-a",
            role="implementer",
            model="model-a",
            started_by=["agent-b"],
            stage="implement",
            attempt=1,
            provider="provider-a",
            reasoning_effort="high",
            harness_version="1.0",
            reply_json='{"kind":"done","value":{}}',
            transcript_length=1024,
            usage=p.TokenUsage(
                input=100,
                cache_read=10,
                cache_creation=20,
                output=30,
                reasoning=5,
                cost_usd=0.25,
                duration_ms=500,
            ),
        ),
        "planning_call": p.PlanningAttrs(call=1),
        "workstream": p.WorkstreamAttrs(
            hypothesis_id="idea-a",
            parent_revision="revision-a",
            base_round=0,
            round=1,
            idea="Improve throughput",
            revision="revision-b",
            outcome="kept",
            summary="Faster",
            failure=p.WorkstreamFailure(
                kind=p.WorkstreamFailureKind.REVIEW_REJECTED,
                role="reviewer",
                turns=2,
                limit=3,
                text="Check failed",
            ),
            files=[p.FileChange(path="src/engine.py", diff_offset=0, diff_length=128)],
            attempts=2,
            profile_skipped=False,
            judge_verdict="passed",
            repeated_error=False,
        ),
        "gate": p.GateAttrs(
            stage=p.GateStage.BENCHMARK,
            subject=p.GateSubject.INPUT,
            recipe="default",
            command="bench --quick",
            reused=True,
            failed=p.FailedCheck(
                check=p.EvidenceKind.BENCHMARK, completed=9, required=10, unit="trials"
            ),
            input_failure=p.InputFailure(
                kind=p.InputFailureKind.BENCHMARK_FAILED,
                stage=p.GateStage.BENCHMARK,
                attempts=2,
                compared=False,
            ),
            source=p.FrameworkSource.GATES,
            source_label="trusted measurement",
        ),
        "adoption": p.AdoptionAttrs(
            candidate="revision-b", selection=p.Selection.CANDIDATE, settlement_id="settlement-a"
        ),
        "comparison": p.ComparisonAttrs(
            metric="throughput",
            unit="requests/s",
            direction=p.Direction.MAX,
            ratio=1.25,
            noise=0.01,
            verdict=p.ComparisonVerdict.BETTER,
            candidate_value=125,
            base_value=100,
            candidate_samples=3,
            base_samples=3,
        ),
        "baseline": p.BaselineAttrs(
            label="Reference",
            status=p.BaselineStatus.MEASURED,
            metric="throughput",
            unit="requests/s",
            direction=p.Direction.MAX,
            value=100,
            spread=0.02,
            samples=3,
            hosts=["host-a"],
            reason="Reference reading",
        ),
        "environment_probe": p.EnvironmentProbeAttrs(
            state=p.ProbeState.QUEUED,
            job_id="job-a",
            pending_reason="Resources",
            waited_seconds=5,
            queue_timeout_seconds=60,
        ),
        "capacity_wait": p.CapacityWaitAttrs(
            provider="provider-a",
            condition=p.CapacityCondition.QUOTA_EXHAUSTED,
            detail="Daily capacity",
            resets_at=p.Timestamp(seconds=100),
            resumes_at=p.Timestamp(seconds=110),
            policy=p.CapacityPolicy.FALLBACK,
            fallback_provider="provider-b",
            fallback_model="model-b",
            wait_resumes_at=p.Timestamp(seconds=120),
            reason="operator",
        ),
        "provider_switch": p.ProviderSwitchAttrs(
            from_provider="provider-a",
            to_provider="provider-b",
            to_model="model-b",
            reason=p.ProviderSwitchReason.POLICY,
            detail="Fallback selected",
        ),
    }


def span_example(kind: str, attrs: Message) -> p.SpanRecord:
    """Pack typed attrs in a complete, ended span suitable for replacement by id."""
    return p.SpanRecord(
        change=p.Change.ENDED,
        span=p.Span(
            id="op:example",
            parent="flow:search#1",
            runtime_type=p.RuntimeType.OPERATION,
            name="example",
            status=p.SpanStatus.DONE,
            started_at=1,
            ended_at=2,
            waiting_on=[p.WaitingOn(kind=p.WaitKind.OPERATION, ref="op:child", since=1)],
            attempts=[
                p.Attempt(
                    n=1,
                    of=2,
                    started_at=1,
                    ended_at=2,
                    outcome=p.AttemptOutcome.DONE,
                    reason="complete",
                )
            ],
            stop=p.Stop(requested_at=1.5, landed=False),
            label=p.Label(
                kind=kind,
                title=kind,
                verdict="passed",
                reason="Complete",
                links=[p.SpanLink(rel="started_by", to="request-a")],
                attrs=Any(
                    type_url="type.googleapis.com/" + attrs.desc().type_name,
                    value=attrs.to_binary(),
                ),
            ),
        ),
    )


def descriptors_for(attrs: dict[str, Message]) -> FileDescriptorSet:
    """Include each attrs file and its transitive imports once, in path order."""
    files: dict[str, DescFile] = {}
    pending = [value.desc().file for value in attrs.values()]
    while pending:
        file = pending.pop()
        if file.name not in files:
            files[file.name] = file
            pending.extend(file.dependencies)
    return FileDescriptorSet(file=[files[name].proto for name in sorted(files)])


def record_examples() -> dict[str, p.Record]:
    """One example per body plus lifecycle and attrs variants, with fixed wall time."""
    blob = p.BlobRef(sha256="a" * 64, size=100000, media_type="text/plain")
    attrs = attrs_examples()
    details = detail_examples()
    epoch = p.EpochStarted(
        descriptors=descriptors_for({**attrs, **details}),
        run_id="run-a",
        epoch=1,
        tree_hash="tree-a",
        dirty=False,
        api_schema_hash="b" * 64,
        store_schema_hash="c" * 64,
        vibesys_version="1.0",
        run=p.RunDescription(
            strategy="search",
            input="revision-a",
            objective=blob,
            objectives=[p.Objective(series="throughput", direction=p.Direction.MAX)],
            correctness_check=["check", "--all"],
            evaluator=p.Evaluator(name="bench", version="1"),
            detail=packed(details["run_description"]),
            expected_roles=["implementer"],
        ),
        span_kinds=[
            p.SpanKindDecl(
                kind=kind,
                attrs_type=value.desc().type_name,
                verdicts=["passed"],
                link_rels=["started_by"],
                timeline=True,
            )
            for kind, value in attrs.items()
        ],
        series=[
            p.MetricSeriesDecl(
                name="throughput",
                unit="requests/s",
                direction=p.Direction.MAX,
                role=p.SeriesRole.OBJECTIVE,
            )
        ],
    )
    bodies = {
        "epoch_started": p.Record(body=p.Oneof("epoch_started", epoch)),
        "declaration_amended": p.Record(
            body=p.Oneof("declaration_amended", p.EpochStarted.from_binary(epoch.to_binary()))
        ),
        "legacy_event": p.Record(
            body=p.Oneof("legacy_event", p.LegacyEvent(json='{"type":"run_finished"}'))
        ),
        "lifecycle": p.Record(
            body=p.Oneof("lifecycle", p.Lifecycle(state=p.Oneof("running", p.Running())))
        ),
        "span": p.Record(body=p.Oneof("span", span_example("agent_turn", attrs["agent_turn"]))),
        "metric": p.Record(
            body=p.Oneof(
                "metric", p.MetricPoint(series="throughput", span="op:gate", value=125, clock=2)
            )
        ),
        "stream": p.Record(
            body=p.Oneof(
                "stream",
                p.StreamRef(
                    id="log-a",
                    kind=p.StreamKind.LOG,
                    span="op:gate",
                    title="Build",
                    owner=p.StreamOwner(
                        kind=p.Oneof(
                            "gate",
                            p.GateStreamOwner(
                                execution="evaluation-a",
                                gate=p.LogGate.BUILD,
                                attempt=1,
                            ),
                        )
                    ),
                    state=p.StreamState.ENDED,
                    length=100,
                ),
            )
        ),
        "blob": p.Record(
            body=p.Oneof("blob", p.BlobAttached(span="op:turn", role="raw_result", blob=blob))
        ),
        "intent": p.Record(
            body=p.Oneof(
                "intent",
                p.IntentState(
                    control_id="request-a",
                    action=p.IntentAction.STEER,
                    step=p.IntentStep.APPLIED,
                    target=p.SteerTarget(kind=p.SteerTargetKind.ROLE, id="implementer"),
                    mode=p.SteerMode.NEXT_TURN,
                ),
            )
        ),
        "notice": p.Record(
            body=p.Oneof(
                "notice",
                p.Notice(
                    severity=p.Severity.WARNING,
                    code=p.NoticeCode.FRAMEWORK_WARNING,
                    message="An optional check failed",
                    span="op:gate",
                ),
            )
        ),
        "core_input": p.Record(
            clock=2,
            body=p.Oneof(
                "core_input", p.CoreInput(schema_version=1, event=b'{"kind":"epoch_resumed"}')
            ),
        ),
        "step_started": p.Record(
            clock=2,
            body=p.Oneof(
                "step_started",
                p.StepStarted(
                    tag="request-a",
                    attempt=2,
                    schema_version=1,
                    request=b'{"kind":"discard_workspace"}',
                    deadline_at=60,
                ),
            ),
        ),
        "step_finished": p.Record(
            clock=3,
            body=p.Oneof(
                "step_finished",
                p.StepFinished(
                    tag="request-a",
                    attempt=2,
                    schema_version=1,
                    result=p.Oneof("cancelled", p.StepCancelReason.DEADLINE),
                ),
            ),
        ),
        "run_ended": p.Record(
            clock=4,
            body=p.Oneof(
                "run_ended",
                p.RunEnded(
                    schema_version=1,
                    result=b'{"outcome":"success","reason":"done","selection":null}',
                ),
            ),
        ),
        "attempt_interrupted": p.Record(
            clock=3,
            body=p.Oneof(
                "attempt_interrupted",
                p.AttemptInterrupted(tag="request-a", attempt=1, reason=p.InterruptionReason.PAUSE),
            ),
        ),
    }
    result = bodies
    for record in result.values():
        record.seq = 1
        record.at = p.Timestamp(seconds=123)
        record.epoch = 1
    for kind, value in attrs.items():
        result["span_" + kind] = p.Record(
            seq=1,
            at=p.Timestamp(seconds=123),
            epoch=1,
            body=p.Oneof("span", span_example(kind, value)),
        )
    chats = {
        "created": p.ChatRecord(
            event=p.Oneof(
                "created",
                p.ChatSessionCreated(
                    id="default", title="Questions", provider="test", model="test"
                ),
            )
        ),
        "message": p.ChatRecord(
            event=p.Oneof(
                "message",
                p.ChatMessage(
                    request_id="q-1", role=p.ChatRole.USER, content=p.Oneof("text", "Why?")
                ),
            )
        ),
        "token": p.ChatRecord(
            event=p.Oneof("token", p.ChatToken(request_id="q-1", text="Because"))
        ),
        "proposed_steer": p.ChatRecord(
            event=p.Oneof(
                "proposed_steer",
                p.ChatSteerProposed(
                    id="proposal-a",
                    request_id="q-1",
                    text="Compare allocations",
                    target=p.SteerTarget(kind=p.SteerTargetKind.RUN),
                    mode=p.SteerMode.NEXT_TURN,
                ),
            )
        ),
    }
    for name, value in chats.items():
        result["chat_" + name] = p.Record(
            seq=1,
            at=p.Timestamp(seconds=123),
            epoch=1,
            body=p.Oneof("chat", value),
        )
    states = {
        "starting": p.Lifecycle(
            state=p.Oneof(
                "starting",
                p.Starting(
                    stage=p.StartupStage.BUILDING_IMAGE,
                    state=p.StageState.RUNNING,
                    progress=p.Progress(done=1, total=2, label="Build"),
                ),
            )
        ),
        "running_hold": p.Lifecycle(
            state=p.Oneof(
                "running", p.Running(hold=p.Hold(reason=p.Oneof("operator", p.OperatorHold())))
            )
        ),
        "draining": p.Lifecycle(state=p.Oneof("draining", p.Draining(goal=p.DrainGoal.PARK))),
        "parked": p.Lifecycle(state=p.Oneof("parked", p.Parked())),
        "ended": p.Lifecycle(
            state=p.Oneof(
                "ended",
                p.Ended(
                    outcome=p.Outcome.FAILED,
                    detail=packed(details["ended"]),
                    summary="No candidate",
                    resumable=True,
                    failure=p.RunFailure(
                        kind=p.RunFailureKind.BUDGET_EXHAUSTED,
                        reason="Budget exhausted",
                        detail=packed(details["run_failure"]),
                    ),
                ),
            )
        ),
    }
    for name, value in states.items():
        result["lifecycle_" + name] = p.Record(
            seq=1,
            at=p.Timestamp(seconds=123),
            epoch=1,
            body=p.Oneof("lifecycle", value),
        )
    return result


def generate(root: Path) -> None:
    """Write validated binary examples; no timestamps or platform-specific inputs."""
    root.mkdir(parents=True, exist_ok=True)
    for name, record in record_examples().items():
        p.validate(record)
        (root / (name + ".pb")).write_bytes(record.to_binary())


if __name__ == "__main__":
    generate(Path(__file__).resolve().parents[1] / "proto/testdata/records")
