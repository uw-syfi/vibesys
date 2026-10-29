"""The run digests and the usage window as registered reads: what the gateway serves the app.

The helper's models stay the definition of what a digest or a window says (``test_run_digest``,
``test_usage_window``); these tests hold the registered reads to the same facts through the public
read entry point, with the request validated as the gateway validates it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from tests.server.host_helper_support import RUN_ID, fake_host, project_with_run

from server.host_api import RUN_EVENTS_FILE, RunDigest, RunsListReply, RunsListRequest
from server.host_helper_requests import answer, registry_read_source
from server.host_operation_reads import answer_registered_read
from vs_project.api import Project
from vs_proto.api import Oneof, ReadRequest, ReadResponse, Timestamp, ValidationError, validate
from vs_proto.api import host_payloads as pb
from vs_sim.api.testing import SimNetwork, SimThreads

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import JsonValue

_T0 = datetime(2026, 10, 10, 12, tzinfo=UTC)


def _at(seconds: int) -> datetime:
    return _T0 + timedelta(seconds=seconds)


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


def _span(kind: str, ident: str, parent: str | None, attrs: dict[str, JsonValue]) -> JsonValue:
    return {
        "id": ident,
        "parent": parent,
        "status": "done",
        "started_at": 1.0,
        "ended_at": 2.0,
        "label": {"kind": kind, "verdict": "passed", "attrs_json": json.dumps(attrs)},
    }


def _kept(index: int, reading: float, landed: int, sequence: int) -> list[bytes]:
    workstream = f"flow:{index}"
    gate = _span("gate", f"op:{index}", workstream, {"metric_value": reading, "vs_baseline": 1.5})
    idea = _span("workstream", workstream, None, {"hypothesis_id": f"h{index}"})
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


def _project(tmp_path: Path) -> Path:
    project = project_with_run(tmp_path / "proj", RUN_ID)
    lines = [_event(1, "run_started", 0, {"overview": {"objective": {"direction": "max"}}})]
    lines += _turn(2, 100, 2.0)
    lines += _kept(0, 150.0, 200, 4)
    lines += _turn(6, 3700, 1.0)
    lines += _kept(1, 180.0, 3800, 8)
    journal = Project.log_directory_for(project.resolve(), RUN_ID) / RUN_EVENTS_FILE
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes(b"".join(lines))
    return project


def _read(envelope: ReadRequest, method: str) -> object:
    """The value of the registered read ``method`` the host answers ``envelope`` with."""
    threads = SimThreads()
    host = fake_host(threads, SimNetwork(threads))
    validate(envelope)
    response = ReadResponse.from_binary(
        answer_registered_read(
            method.replace("_", "-"), envelope.to_binary(), registry_read_source(host)
        )
    )
    validate(response)
    assert response.result is not None
    assert response.result.field == method, response.result
    return response.result.value


def _digests(project: Path, run_ids: list[str], *, dashboard: bool) -> pb.ListRunDigestsResponse:
    request = pb.ListRunDigestsRequest(
        request_id="digests", project=str(project), run_ids=run_ids, with_dashboard=dashboard
    )
    result = _read(
        ReadRequest(checkout="/checkout", method=Oneof("list_run_digests", request)),
        "list_run_digests",
    )
    assert isinstance(result, pb.ListRunDigestsResponse)
    return result


def _usage(project: Path, run_ids: list[str], since: int, until: int) -> pb.UsageWindowResponse:
    request = pb.UsageWindowRequest(
        request_id="usage",
        project=str(project),
        run_ids=run_ids,
        since=Timestamp.from_datetime(_at(since)),
        until=Timestamp.from_datetime(_at(until)),
    )
    result = _read(
        ReadRequest(checkout="/checkout", method=Oneof("usage_window", request)), "usage_window"
    )
    assert isinstance(result, pb.UsageWindowResponse)
    return result


def test_digests_say_what_the_journal_says_and_carry_the_tile_only_when_asked(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)

    plain = _digests(project, [RUN_ID], dashboard=False)
    tile = _digests(project, [RUN_ID], dashboard=True)

    assert plain.request_id == "digests"
    [entry] = plain.digests
    assert entry.run_id == RUN_ID
    assert entry.result is not None
    assert entry.result.field == "digest"
    assert isinstance(entry.result.value, pb.RunDigest)
    assert entry.result.value.ideas == 2
    assert not entry.result.value.has_field("dashboard")
    [with_tile] = tile.digests
    assert with_tile.result is not None
    assert with_tile.result.field == "digest"
    assert isinstance(with_tile.result.value, pb.RunDigest)
    dashboard = with_tile.result.value.dashboard
    assert dashboard is not None
    assert [point.verdict for point in dashboard.points] == [pb.IdeaVerdict.KEPT] * 2
    assert dashboard.points_omitted == 0
    assert dashboard.attention == pb.RunAttention.NONE


def test_digests_name_only_the_runs_the_project_has_and_a_journal_holds(tmp_path: Path) -> None:
    project = _project(tmp_path)

    answered = _digests(project, ["20200101-000000-00000000-dynamic", RUN_ID], dashboard=False)

    assert [entry.run_id for entry in answered.digests] == [RUN_ID]


def test_the_digest_restates_the_helpers_listing(tmp_path: Path) -> None:
    project = _project(tmp_path)
    threads = SimThreads()
    helper = answer(
        RunsListRequest(
            request_id="l",
            deadline_ms=5000,
            project=str(project),
            digest_runs=(RUN_ID,),
            with_dashboard=True,
        ),
        fake_host(threads, SimNetwork(threads)),
        threads.event(),
    )
    assert isinstance(helper, RunsListReply)
    served = _digests(project, [RUN_ID], dashboard=True)

    [summary] = helper.runs.runs
    [entry] = served.digests
    assert isinstance(summary.digest, RunDigest)
    assert entry.result is not None
    assert isinstance(entry.result.value, pb.RunDigest)
    digest = entry.result.value
    assert (digest.ideas, digest.duration_s) == (summary.digest.ideas, summary.digest.duration_s)
    assert entry.created_at == Timestamp.from_datetime(summary.created_at)


def test_the_window_prices_the_turns_and_counts_the_bests_that_landed_in_it(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)

    early = _usage(project, [RUN_ID], 0, 3600)
    both = _usage(project, [RUN_ID, "20200101-000000-00000000-dynamic"], 0, 7200)

    assert (early.spent_usd, early.new_bests, early.cost_per_new_best_usd) == (2.0, 1, 2.0)
    assert (both.spent_usd, both.new_bests, both.turns) == (3.0, 2, 2)
    assert both.tokens is not None
    assert both.tokens.output == 8
    assert not both.tokens.has_field("reasoning")
    assert [(row.run_id, row.cost_usd) for row in both.by_run] == [(RUN_ID, 3.0)]
    assert [(a.role, a.harness, a.model, a.turns) for a in both.by_agent] == [
        ("implementer", "codex", "gpt-x", 2)
    ]
    assert list(both.missing_runs) == ["20200101-000000-00000000-dynamic"]
    assert both.since == Timestamp.from_datetime(_at(0))


@pytest.mark.parametrize(
    ("since", "until"),
    [(100, 100), (200, 100), (0, 337 * 3600)],
    ids=["empty", "backwards", "longer than two weeks"],
)
def test_a_window_the_helper_would_refuse_is_refused_by_the_message(
    tmp_path: Path, since: int, until: int
) -> None:
    request = pb.UsageWindowRequest(
        request_id="usage",
        project=str(tmp_path),
        since=Timestamp.from_datetime(_at(since)),
        until=Timestamp.from_datetime(_at(until)),
    )

    with pytest.raises(ValidationError):
        validate(request)
