"""``usage.window``: what a project's runs spent in a window, and who spent it.

The aggregation (``usage_window``) is held to a plain statement of the answer over generated
records; the request through ``answer`` runs over journals in a project, so the usage records and
the landing times of kept ideas are the ones the helper reads from disk.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.server.host_helper_support import RUN_ID, fake_host, project_with_run

from server.host_api import (
    HELPER_LINE_MAX_BYTES,
    RUN_EVENTS_FILE,
    HelperError,
    HelperFeature,
    RunsListReply,
    RunsListRequest,
    parse_helper_message,
    parse_helper_request,
    request_features,
)
from server.host_helper_requests import answer
from server.host_run_summary import (
    RATE_WINDOW_S,
    USAGE_WINDOW_MAX_AGENTS,
    USAGE_WINDOW_MAX_HOURS,
    USAGE_WINDOW_MAX_RUNS,
    TokenTally,
    UsageRecord,
    UsageWindowReply,
    UsageWindowRequest,
)
from server.usage_window import RunUsage, usage_window
from vs_decode.api import Unreadable
from vs_project.api import Project
from vs_sim.api.testing import SimNetwork, SimThreads

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path
    from random import Random

    from pydantic import JsonValue

_T0 = datetime(2026, 10, 10, 12, tzinfo=UTC)
_SPAN_S = 5 * 24 * 3600
"""Records and bests fall within five days of ``_T0``; windows are drawn inside and around it."""
_NO_TOKENS = TokenTally(input=None, cache_read=None, cache_write=None, output=None, reasoning=None)


def _at(seconds: int) -> datetime:
    return _T0 + timedelta(seconds=seconds)


def _record(
    ended: int | None,
    cost: float | None,
    agent: tuple[str | None, str | None, str | None] = ("implementer", "codex", "gpt"),
    output: int | None = 5,
) -> UsageRecord:
    return UsageRecord(
        run_id=RUN_ID,
        execution_id=None,
        span_id=None,
        round_label=None,
        role=agent[0],
        harness=agent[1],
        model=agent[2],
        reasoning_effort=None,
        harness_version=None,
        started_at=None,
        ended_at=None if ended is None else _at(ended),
        tokens=TokenTally(
            input=None, cache_read=None, cache_write=None, output=output, reasoning=None
        ),
        cost_usd=cost,
        duration_ms=None,
    )


_AGENTS = st.tuples(
    st.sampled_from(["planner", "implementer", None]),
    st.sampled_from(["codex", "claude", None]),
    st.sampled_from(["m1", "m2", None]),
)
_RECORDS = st.builds(
    _record,
    st.none() | st.integers(0, _SPAN_S),
    st.none() | st.floats(0, 50, allow_nan=False),
    _AGENTS,
    st.none() | st.integers(0, 10_000),
)
_RUNS = st.lists(
    st.builds(
        RunUsage,
        st.uuids().map(str),
        st.none() | st.text(min_size=1, max_size=8),
        st.lists(_RECORDS, max_size=8),
        st.lists(st.integers(0, _SPAN_S).map(_at), max_size=4),
    ),
    max_size=6,
    unique_by=lambda run: run.run_id,
)
_WINDOWS = st.tuples(st.integers(-3600, _SPAN_S), st.integers(1, USAGE_WINDOW_MAX_HOURS * 3600))


def _request(since: int, length: int, run_ids: tuple[str, ...] = ()) -> UsageWindowRequest:
    return UsageWindowRequest(
        request_id="u",
        deadline_ms=5000,
        project="/p",
        run_ids=run_ids,
        since=_at(since),
        until=_at(since + length),
    )


def _inside(run: RunUsage, since: int, length: int) -> list[UsageRecord]:
    return [
        record
        for record in run.records
        if record.ended_at is not None and _at(since) <= record.ended_at < _at(since + length)
    ]


@given(runs=_RUNS, window=_WINDOWS)
def test_the_window_counts_exactly_the_turns_that_ended_in_it(
    runs: list[RunUsage], window: tuple[int, int]
) -> None:
    since, length = window
    reply = usage_window(_request(since, length), runs, ())

    inside = [(run, record) for run in runs for record in _inside(run, since, length)]
    assert reply.turns == len(inside)
    assert reply.unpriced_turns == sum(record.cost_usd is None for _, record in inside)
    assert reply.untimed_turns == sum(
        record.ended_at is None for run in runs for record in run.records
    )
    assert reply.spent_usd == math.fsum(record.cost_usd or 0.0 for _, record in inside)
    assert reply.tokens.output == (
        None
        if all(record.tokens.output is None for _, record in inside)
        else sum(record.tokens.output or 0 for _, record in inside)
    )


@given(runs=_RUNS, window=_WINDOWS)
def test_the_unpriced_turns_are_placed_on_the_runs_and_agents_that_ran_them(
    runs: list[RunUsage], window: tuple[int, int]
) -> None:
    since, length = window
    reply = usage_window(_request(since, length), runs, ())

    assert sum(each.unpriced_turns for each in reply.by_run) == reply.unpriced_turns
    assert sum(each.unpriced_turns for each in reply.by_agent) == reply.unpriced_turns
    for spend in reply.by_run:
        run = next(each for each in runs if each.run_id == spend.run_id)
        assert spend.unpriced_turns == sum(
            record.cost_usd is None for record in _inside(run, since, length)
        )
    assert all(each.unpriced_turns <= each.turns for each in reply.by_agent)


@given(runs=_RUNS, window=_WINDOWS)
def test_the_costs_by_run_and_by_agent_are_the_parts_of_the_total(
    runs: list[RunUsage], window: tuple[int, int]
) -> None:
    since, length = window
    reply = usage_window(_request(since, length), runs, ())

    assert math.isclose(sum(each.cost_usd for each in reply.by_run), reply.spent_usd, abs_tol=1e-9)
    assert reply.agents_omitted == 0
    assert math.isclose(
        sum(each.cost_usd for each in reply.by_agent), reply.spent_usd, abs_tol=1e-9
    )
    assert sum(each.turns for each in reply.by_agent) == reply.turns
    assert [each.cost_usd for each in reply.by_run] == sorted(
        (each.cost_usd for each in reply.by_run), reverse=True
    )
    assert {each.run_id for each in reply.by_run} == {
        run.run_id for run in runs if _inside(run, since, length)
    }


@given(runs=_RUNS, window=_WINDOWS, shuffle=st.randoms(use_true_random=False))
def test_the_answer_does_not_depend_on_the_order_of_runs_or_records(
    runs: list[RunUsage], window: tuple[int, int], shuffle: Random
) -> None:
    since, length = window
    shuffled = [
        RunUsage(
            run.run_id, run.task, _shuffled(shuffle, run.records), _shuffled(shuffle, run.bests)
        )
        for run in runs
    ]

    assert usage_window(_request(since, length), _shuffled(shuffle, shuffled), ()) == usage_window(
        _request(since, length), runs, ()
    )


def _shuffled[Item](shuffle: Random, items: Sequence[Item]) -> list[Item]:
    mixed = list(items)
    shuffle.shuffle(mixed)
    return mixed


@given(runs=_RUNS, window=_WINDOWS, cut=st.floats(0.01, 0.99))
def test_cutting_a_window_in_two_splits_its_turns_and_its_spend(
    runs: list[RunUsage], window: tuple[int, int], cut: float
) -> None:
    since, length = window
    first = min(length - 1, max(1, int(length * cut)))
    whole = usage_window(_request(since, length), runs, ()) if length > 1 else None
    if whole is None:
        return
    left = usage_window(_request(since, first), runs, ())
    right = usage_window(_request(since + first, length - first), runs, ())

    assert left.turns + right.turns == whole.turns
    assert left.new_bests + right.new_bests == whole.new_bests
    assert math.isclose(left.spent_usd + right.spent_usd, whole.spent_usd, abs_tol=1e-9)


@given(runs=_RUNS, window=_WINDOWS)
def test_a_new_best_is_a_kept_idea_that_landed_in_the_window(
    runs: list[RunUsage], window: tuple[int, int]
) -> None:
    since, length = window
    reply = usage_window(_request(since, length), runs, ())

    landed = sum(_at(since) <= best < _at(since + length) for run in runs for best in run.bests)
    assert reply.new_bests == landed
    if landed == 0:
        assert reply.cost_per_new_best_usd is None
    else:
        assert reply.cost_per_new_best_usd == pytest.approx(reply.spent_usd / landed)


@given(
    runs=_RUNS, since=st.integers(0, _SPAN_S), length=st.integers(1, USAGE_WINDOW_MAX_HOURS * 3600)
)
def test_the_rate_is_the_trailing_hour_per_hour_or_the_whole_short_window(
    runs: list[RunUsage], since: int, length: int
) -> None:
    reply = usage_window(_request(since, length), runs, ())

    span = min(RATE_WINDOW_S, length)
    recent = math.fsum(
        record.cost_usd or 0.0
        for run in runs
        for record in _inside(run, since + length - span, span)
    )
    assert reply.spend_per_hour_usd == pytest.approx(recent / (span / 3600))


def test_the_rate_is_the_last_hour_of_the_window_it_is_given() -> None:
    """Spend an hour apart: the rate is of the last hour only, and of a short window the whole."""
    runs = [RunUsage("a", None, [_record(100, 3.0), _record(3600 + 100, 1.0)], [])]

    reply = usage_window(_request(0, 7200), runs, ())

    assert reply.spent_usd == 4.0
    assert reply.spend_per_hour_usd == 1.0
    assert usage_window(_request(0, 3600), runs, ()).spend_per_hour_usd == 3.0
    assert usage_window(_request(0, 1800), runs, ()).spend_per_hour_usd == 6.0


def test_a_turn_with_no_end_time_is_counted_apart_and_never_in_a_window() -> None:
    runs = [RunUsage("a", "t", [_record(None, 9.0), _record(10, 1.0)], [])]

    reply = usage_window(_request(0, 3600), runs, ())

    assert (reply.untimed_turns, reply.turns, reply.spent_usd) == (1, 1, 1.0)


def test_a_turn_at_the_start_is_in_the_window_and_one_at_the_end_is_not() -> None:
    runs = [RunUsage("a", None, [_record(0, 1.0), _record(3600, 2.0)], [_at(0), _at(3600)])]

    reply = usage_window(_request(0, 3600), runs, ())

    assert (reply.turns, reply.spent_usd, reply.new_bests) == (1, 1.0, 1)


def test_agents_beyond_the_cap_are_counted_and_the_costliest_stay() -> None:
    many = USAGE_WINDOW_MAX_AGENTS + 5
    records = [_record(10, float(at), ("r", "h", f"model-{at:03d}")) for at in range(many)]

    reply = usage_window(_request(0, 3600), [RunUsage("a", None, records, [])], ())

    assert len(reply.by_agent) == USAGE_WINDOW_MAX_AGENTS
    assert reply.agents_omitted == 5
    assert reply.by_agent[0].model == f"model-{many - 1:03d}"
    assert reply.spent_usd == math.fsum(range(many))


def test_the_largest_reply_stays_far_under_a_helper_line() -> None:
    """The most runs and agents with the longest names and numbers fit a line with room to spare."""
    long = "x" * 200
    records = [
        _record(10, 1 / 3, (f"role-{long}{at}", long, f"{long}{at}"), 123_456_789)
        for at in range(USAGE_WINDOW_MAX_AGENTS * 2)
    ]
    ids = [f"{at:040d}" for at in range(USAGE_WINDOW_MAX_RUNS)]
    runs = [RunUsage(run_id, long, records, []) for run_id in ids]
    request = _request(0, 3600, tuple(ids))

    reply = usage_window(request, runs, ids)

    assert len(reply.by_run) == USAGE_WINDOW_MAX_RUNS
    assert len(reply.by_agent) == USAGE_WINDOW_MAX_AGENTS
    assert len(reply.model_dump_json()) < HELPER_LINE_MAX_BYTES // 4
    assert len(request.model_dump_json()) < HELPER_LINE_MAX_BYTES // 4


@given(since=st.integers(0, _SPAN_S), length=st.integers(-10_000, 400 * 3600))
def test_a_window_is_bounded_and_runs_forward(since: int, length: int) -> None:
    if 0 < length <= USAGE_WINDOW_MAX_HOURS * 3600:
        assert _request(since, length).until > _request(since, length).since
    else:
        with pytest.raises(ValidationError):
            _request(since, length)


def test_more_runs_than_the_cap_are_refused() -> None:
    ids = tuple(f"r{at}" for at in range(USAGE_WINDOW_MAX_RUNS + 1))

    with pytest.raises(ValidationError):
        _request(0, 3600, ids)


def test_the_method_needs_its_feature_and_the_messages_are_strict() -> None:
    request = _request(0, 3600, (RUN_ID,))
    line = request.model_dump_json().encode()

    assert request_features(request) == (HelperFeature.USAGE_WINDOW,)
    assert parse_helper_request(line) == request
    unknown = json.loads(line) | {"unexpected": 1}
    assert isinstance(parse_helper_request(json.dumps(unknown).encode()), Unreadable)
    naive = json.loads(line) | {"since": "2026-10-10T12:00:00"}
    assert isinstance(parse_helper_request(json.dumps(naive).encode()), Unreadable)

    reply = usage_window(request, [RunUsage(RUN_ID, None, [_record(10, 1.0)], [])], ())
    assert parse_helper_message(reply.model_dump_json().encode()) == reply


# One run on disk: two turns an hour apart, and a kept idea landing after each.


def _event(
    sequence: int, kind: str, at: int, data: dict[str, JsonValue], **top: JsonValue
) -> bytes:
    document: dict[str, JsonValue] = {
        "protocol_version": 1,
        "sequence": sequence,
        "run_id": RUN_ID,
        "timestamp": _at(at).isoformat(),
        "type": kind,
        "text": "",
        "data": {"kind": kind, **data},
        **top,
    }
    return json.dumps(document).encode() + b"\n"


def _span(
    kind: str, ident: str, parent: str | None, verdict: str, attrs: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    return {
        "id": ident,
        "parent": parent,
        "status": "done",
        "started_at": 1.0,
        "ended_at": 2.0,
        "label": {"kind": kind, "verdict": verdict, "attrs_json": json.dumps(attrs)},
    }


def _kept(index: int, reading: float, landed: int, sequence: int) -> list[bytes]:
    workstream = f"flow:{index}"
    gate = _span(
        "gate",
        f"op:{index}",
        workstream,
        "passed",
        {"metric_value": reading, "vs_baseline": reading / 100.0},
    )
    idea = _span("workstream", workstream, None, "passed", {"hypothesis_id": f"h{index}"})
    return [
        _event(sequence, "span_ended", landed, {"span": gate}),
        _event(sequence + 1, "span_ended", landed + 1, {"span": idea}),
    ]


def _turn(sequence: int, at: int, cost: float) -> list[bytes]:
    return [
        _event(
            sequence,
            "agent_execution_started",
            at - 5,
            {"provider": "codex", "model": "gpt-x"},
            execution_id=f"e{sequence}",
            agent_kind="implementer",
        ),
        _event(
            sequence + 1,
            "turn_usage_recorded",
            at,
            {"input_tokens": 10, "output_tokens": 4, "total_cost_usd": cost},
            execution_id=f"e{sequence}",
        ),
    ]


def _journal(project: Path, lines: list[bytes]) -> None:
    journal = Project.log_directory_for(project.resolve(), RUN_ID) / RUN_EVENTS_FILE
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes(b"".join(lines))


def _ask(project: Path, run_ids: tuple[str, ...], since: int, length: int) -> UsageWindowReply:
    threads = SimThreads()
    host = fake_host(threads, SimNetwork(threads))
    request = UsageWindowRequest(
        request_id="u",
        deadline_ms=5000,
        project=str(project),
        run_ids=run_ids,
        since=_at(since),
        until=_at(since + length),
    )
    reply = answer(request, host, threads.event())
    assert isinstance(reply, UsageWindowReply), reply
    return reply


def _task_of(project: Path) -> str | None:
    """The task ``runs.list`` names for the project's one run."""
    threads = SimThreads()
    host = fake_host(threads, SimNetwork(threads))
    listed = answer(
        RunsListRequest(request_id="l", deadline_ms=5000, project=str(project)),
        host,
        threads.event(),
    )
    assert isinstance(listed, RunsListReply)
    return listed.runs.runs[0].task


def test_the_helper_prices_the_window_from_the_journal_and_counts_the_bests_that_landed_in_it(
    tmp_path: Path,
) -> None:
    project = project_with_run(tmp_path / "proj", RUN_ID)
    lines = [_event(1, "run_started", 0, {"overview": {"objective": {"direction": "max"}}})]
    lines += _turn(2, 100, 2.0)
    lines += _kept(0, 150.0, 200, 4)
    lines += _turn(6, 3700, 1.0)
    lines += _kept(1, 180.0, 3800, 8)
    _journal(project, lines)

    early = _ask(project, (RUN_ID,), 0, 3600)
    late = _ask(project, (RUN_ID,), 3600, 3600)
    both = _ask(project, (RUN_ID, RUN_ID), 0, 7200)

    assert (early.spent_usd, early.new_bests, early.cost_per_new_best_usd) == (2.0, 1, 2.0)
    assert (late.spent_usd, late.new_bests, late.cost_per_new_best_usd) == (1.0, 1, 1.0)
    assert (both.spent_usd, both.new_bests, both.cost_per_new_best_usd) == (3.0, 2, 1.5)
    assert both.tokens.output == 8
    assert [(each.run_id, each.task, each.cost_usd) for each in both.by_run] == [
        (RUN_ID, _task_of(project), 3.0)
    ]
    assert [(a.role, a.harness, a.model, a.cost_usd, a.turns) for a in both.by_agent] == [
        ("implementer", "codex", "gpt-x", 3.0, 2)
    ]


def test_a_run_the_project_does_not_have_is_reported_and_adds_nothing(tmp_path: Path) -> None:
    project = project_with_run(tmp_path / "proj", RUN_ID)
    _journal(project, _turn(1, 100, 2.0))

    reply = _ask(project, ("20200101-000000-00000000-dynamic", RUN_ID), 0, 3600)

    assert reply.missing_runs == ("20200101-000000-00000000-dynamic",)
    assert reply.spent_usd == 2.0


def test_a_run_with_no_journal_on_this_machine_is_missing(tmp_path: Path) -> None:
    project = project_with_run(tmp_path / "proj", RUN_ID)

    reply = _ask(project, (RUN_ID,), 0, 3600)

    assert reply.missing_runs == (RUN_ID,)
    assert reply.by_run == ()


def test_an_unfound_project_is_an_error_not_an_empty_answer(tmp_path: Path) -> None:
    threads = SimThreads()
    host = fake_host(threads, SimNetwork(threads))
    request = UsageWindowRequest(
        request_id="u",
        deadline_ms=5000,
        project=str(tmp_path / "nowhere"),
        run_ids=(),
        since=_at(0),
        until=_at(60),
    )

    assert isinstance(answer(request, host, threads.event()), HelperError)
