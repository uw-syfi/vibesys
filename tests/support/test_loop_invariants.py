"""The loop invariants flag exactly the violating records."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st
from tests.support.loop_invariants import Invariant, RunRecords, check, summarize

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.loop_invariants import Record

_T0 = "2026-10-03T18:00:00+00:00"


def _event(kind: str, seconds: int = 0, **fields: object) -> dict[str, object]:
    data = fields.pop("data", None)
    return {
        "type": kind,
        "timestamp": f"2026-10-03T18:00:{seconds:02d}+00:00",
        "data": data,
        **fields,
    }


def _started() -> dict[str, object]:
    return _event("run_started", data={"kind": "run_started"})


def _finished(status: str = "completed", seconds: int = 30) -> dict[str, object]:
    return _event("run_finished", seconds, status=status)


def _workstream_state(workstreams: int = 1, profiles: list[dict] | None = None) -> dict:
    return {
        "workstreams": [{"hypothesis_id": f"h{i}"} for i in range(workstreams)],
        "profiles": profiles or [],
    }


def _profile(identifier: str, planning_call: int, status: str | None) -> dict[str, object]:
    outcome = {"status": status} if status is not None else None
    return {"profile_id": identifier, "planning_call": planning_call, "outcome": outcome}


def _tool(call_id: str, tool: str, *, content: str | None, error: bool = False) -> list[Record]:
    call = _event("tool_call", data={"tool": tool, "call_id": call_id})
    if content is None:
        return [call]
    result = _event(
        "tool_result",
        data={"tool": tool, "call_id": call_id, "content": content, "is_error": error},
    )
    return [call, result]


def _invariants(records: RunRecords, stop_grace_s: float | None = None) -> list[Invariant]:
    return [violation.invariant for violation in check(records, stop_grace_s=stop_grace_s)]


def test_a_clean_run_has_no_violations() -> None:
    events = [
        _started(),
        *_tool("c1", "mcp__vs-evaluation__submit_evaluation", content='{"handle_id": "e"}'),
        _event("agent_execution_finished", agent_kind="dynamic-implementer"),
        _finished(),
    ]
    records = RunRecords(
        events=events,
        state=_workstream_state(profiles=[_profile("p1", 1, "observed")]),
        usage=[{"kind": "dynamic-implementer", "input_tokens": 10, "output_tokens": 2}],
        cluster_jobs={"5000": "COMPLETED 0:0"},
    )

    assert check(records) == []
    assert summarize(records).line() == (
        "status=completed wall=30 s turns=1 input_tokens=10 output_tokens=2 cost=$0.000"
    )


def test_a_run_without_one_typed_terminal_event_is_flagged() -> None:
    assert _invariants(RunRecords(events=[_started()])) == [Invariant.TERMINAL_STATUS]
    assert _invariants(RunRecords(events=[_finished("active")])) == [Invariant.TERMINAL_STATUS]


def test_a_completed_run_with_zero_workstreams_is_flagged() -> None:
    records = RunRecords(events=[_finished()], state=_workstream_state(workstreams=0))

    assert _invariants(records) == [Invariant.EMPTY_COMPLETION]


def test_profiles_planned_after_an_unsupported_profile_are_flagged() -> None:
    profiles = [_profile("p1", 1, "unsupported"), _profile("p2", 2, "unsupported")]
    records = RunRecords(events=[_finished()], state=_workstream_state(profiles=profiles))

    violations = check(records)

    assert [v.invariant for v in violations] == [Invariant.CAPABILITY_UNSERVED]
    assert "p2" in violations[0].detail


def test_profiles_that_only_ever_failed_are_flagged() -> None:
    profiles = [_profile("p1", 1, "failed"), _profile("p2", 1, "failed")]
    records = RunRecords(events=[_finished()], state=_workstream_state(profiles=profiles))

    assert _invariants(records) == [Invariant.CAPABILITY_UNSERVED]


def test_a_tool_whose_every_call_is_refused_as_unservable_is_flagged() -> None:
    refusal = "this run's evaluation executor cannot produce evidence kind: profile"
    events = [
        *_tool("c1", "mcp__vs-evaluation__submit_evaluation", content=refusal, error=True),
        _finished(),
    ]

    assert _invariants(RunRecords(events=events)) == [Invariant.CAPABILITY_UNSERVED]


def test_client_side_tool_timeouts_and_unanswered_calls_are_flagged() -> None:
    events = [
        *_tool(
            "c1",
            "mcp__vs-evaluation__await_evaluation",
            content="MCP error -32001: Request timed out",
            error=True,
        ),
        *_tool("c2", "mcp__vs-evaluation__await_evaluation", content=None),
        _finished(),
    ]

    assert _invariants(RunRecords(events=events)) == [Invariant.TOOL_TIMEOUT] * 2


def test_a_prompt_naming_a_missing_run_path_is_flagged(tmp_path: Path) -> None:
    present = tmp_path / "present.md"
    present.write_text("", encoding="utf-8")
    missing = tmp_path / "notes" / "missing.json"
    prompt = (
        f"Read `{present}` and {missing}. Ignore /usr/share/dict/words. "
        "Edit `primes.py`; see `notes/plan.md` and call `count_primes(10)`."
    )
    events = [
        _event("agent_execution_started", data={"user_prompt": prompt}, agent_kind="planner"),
        _finished(),
    ]

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "primes.py").write_text("", encoding="utf-8")

    def exists(path: Path) -> bool:
        return (workspace / path).exists()

    flagged = sorted(
        v.detail.rsplit(" ", 1)[1]
        for v in check(RunRecords(events=events), roots=[tmp_path], exists=exists)
        if v.invariant is Invariant.MISSING_PROMPT_PATH
    )

    assert flagged == sorted([str(missing), "notes/plan.md"])
    # Without the turn's workspace, only absolute paths are checked.
    default = check(RunRecords(events=events), roots=[tmp_path])
    assert [str(missing) in v.detail for v in default] == [True]


def test_work_after_a_stop_and_an_overrun_are_flagged() -> None:
    submitted = {"operation_kind": "evaluation", "operation_id": "e1", "state": "submitted"}
    events = [
        _event("stop_requested", 10),
        _event("async_operation_lifecycle", 12, data=submitted),
        _finished("cancelled", 50),
    ]

    assert _invariants(RunRecords(events=events), stop_grace_s=30.0) == [
        Invariant.EVALUATION_AFTER_STOP,
        Invariant.STOP_OVERRAN,
    ]


_LEDGER_LINES = st.sampled_from(
    ["", "PENDING", "RUNNING", "COMPLETED 0:0", "FAILED 1:0", "CANCELLED 0:0"]
)


@given(st.dictionaries(st.from_regex(r"\A5[0-9]{3}\Z"), _LEDGER_LINES, max_size=8))
def test_exactly_the_pending_or_running_jobs_are_flagged(jobs: dict[str, str]) -> None:
    records = RunRecords(events=[_finished()], cluster_jobs=jobs)

    flagged = [v for v in check(records) if v.invariant is Invariant.CLUSTER_JOB_LEFT]

    assert len(flagged) == sum(line in {"", "PENDING", "RUNNING"} for line in jobs.values())


_ROLES = st.sampled_from(["dynamic-orchestrator", "dynamic-implementer", "dynamic-judge"])


@given(st.lists(_ROLES, max_size=6), st.lists(_ROLES, max_size=6))
def test_a_role_with_fewer_usage_rows_than_finished_turns_is_flagged(
    finished: list[str], recorded: list[str]
) -> None:
    events = [_event("agent_execution_finished", agent_kind=role) for role in finished]
    records = RunRecords(events=[*events, _finished()], usage=[{"kind": r} for r in recorded])

    flagged = {
        v.detail.split(":")[0] for v in check(records) if v.invariant is Invariant.USAGE_UNRECORDED
    }

    assert flagged == {
        role for role in set(finished) if recorded.count(role) < finished.count(role)
    }


def test_records_load_from_a_run_layout(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "core-events.jsonl").write_text(json.dumps(_finished()) + "\n", encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text(json.dumps(_workstream_state(workstreams=0)), encoding="utf-8")
    candidate_logs = tmp_path / "runtime" / "workspaces" / "m-h1" / "logs"
    candidate_logs.mkdir(parents=True)
    (candidate_logs / "usage.jsonl").write_text('{"kind": "dynamic-judge"}\n', encoding="utf-8")
    jobs = tmp_path / "cluster" / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "5000").write_text("PENDING", encoding="utf-8")
    (jobs / "5000.request.json").write_text("[]", encoding="utf-8")

    records = RunRecords.load(logs, state, tmp_path / "cluster")

    assert _invariants(records) == [Invariant.EMPTY_COMPLETION, Invariant.CLUSTER_JOB_LEFT]
    assert records.usage == [{"kind": "dynamic-judge"}]
    assert records.cluster_jobs == {"5000": "PENDING"}


def _envelope(
    status: str = "terminal",
    outcome: str | None = "success",
    hypotheses: int = 1,
    submitted_at: list[float] | None = None,
) -> dict[str, object]:
    intents = [
        {"request": {"kind": "submit_measurement", "plan": {"submitted_at": at}}}
        for at in submitted_at or []
    ]
    return {
        "core": {
            "run": {"status": status, "result": {"outcome": outcome} if outcome else None},
            "intents": {"intents": intents},
        },
        "strategy": {"hypotheses": [{"hypothesis_id": f"h{i}"} for i in range(hypotheses)]},
    }


def _violations(events: list[Record], envelope: dict[str, object]) -> set[Invariant]:
    return {v.invariant for v in check(RunRecords.from_core(events, envelope))}


def test_a_core_run_that_completes_with_a_success_record_passes() -> None:
    assert _violations([_started(), _finished()], _envelope()) == set()


def test_a_core_run_that_completes_without_a_hypothesis_is_empty() -> None:
    assert _violations([_started(), _finished()], _envelope(hypotheses=0)) == {
        Invariant.EMPTY_COMPLETION
    }


@given(
    status=st.sampled_from(["completed", "failed"]),
    recorded=st.sampled_from(["running", "closing", "terminal"]),
    outcome=st.sampled_from(["success", "failed", "cancelled"]),
)
def test_a_terminal_event_must_agree_with_the_core_record(
    status: str, recorded: str, outcome: str
) -> None:
    agrees = recorded == "terminal" and (outcome == "success") == (status == "completed")

    found = _violations([_started(), _finished(status)], _envelope(recorded, outcome))

    assert (Invariant.RECORD_DISAGREES in found) is (not agrees)


def test_a_stop_may_leave_the_core_record_open_or_cancelled() -> None:
    stopped = _event("stopped", 20, status="interrupted")
    stop = _event("stop_requested", 10)
    for envelope in (_envelope("running", None), _envelope("terminal", "cancelled")):
        assert _violations([_started(), stop, stopped], envelope) == set()
    assert _violations([_started(), stop, stopped], _envelope("terminal", "success")) == {
        Invariant.RECORD_DISAGREES
    }


def test_a_measurement_submitted_after_the_stop_request_is_flagged() -> None:
    stop_at = datetime.fromisoformat("2026-10-03T18:00:10+00:00").timestamp()
    events = [
        _started(),
        _event("stop_requested", 10),
        _event("stopped", 20, status="interrupted"),
    ]

    before = _violations(events, _envelope("running", None, submitted_at=[stop_at - 1]))
    after = _violations(events, _envelope("running", None, submitted_at=[stop_at + 1]))

    assert before == set()
    assert after == {Invariant.EVALUATION_AFTER_STOP}
