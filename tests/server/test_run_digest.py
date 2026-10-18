"""A run's digest: the figures a list row shows, folded from its journal and read through the helper."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from hypothesis import example, given
from hypothesis import strategies as st
from tests.server.host_helper_support import RUN_ID, fake_host, project_with_run

from server.host_api import (
    RUN_EVENTS_FILE,
    RunClosing,
    RunDigest,
    RunDigestUnreadable,
    RunEndingFact,
    RunEndOutcome,
    RunsListReply,
    RunsListRequest,
    request_features,
)
from server.host_helper_os import FileProjects
from server.host_helper_requests import answer
from server.run_digest import digest_journal
from vs_project.api import Project
from vs_project.api.testing import FakeGitRepositories
from vs_sim.api.testing import SimNetwork, SimThreads

if TYPE_CHECKING:
    from pydantic import JsonValue


_LEGACY_FIXTURE = (
    Path(__file__).parents[1] / "conformance" / "fixtures" / "run-summary" / "predates-trace.json"
)


def _stamp(seconds: float) -> str:
    minutes, rest = divmod(seconds, 60)
    return f"2026-10-17T03:{int(minutes):02}:{rest:09.6f}Z"


def _event(sequence: int, kind: str, at: float, data: dict[str, JsonValue] | None = None) -> bytes:
    document = {
        "protocol_version": 1,
        "sequence": sequence,
        "run_id": RUN_ID,
        "timestamp": _stamp(at),
        "type": kind,
        "text": "",
        "data": {"kind": kind, **(data or {})},
    }
    return json.dumps(document).encode() + b"\n"


def _span(sequence: int, at: float, span_id: str, label: dict[str, JsonValue]) -> bytes:
    return _event(sequence, "span_ended", at, {"span": {"id": span_id, "label": label}})


def _label(kind: str, verdict: str | None, attrs: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {"kind": kind, "verdict": verdict, "attrs_json": json.dumps(attrs)}


@dataclass(frozen=True)
class _Gate:
    verdict: str
    value: float
    ratio: float


_GATES = st.builds(
    _Gate,
    st.sampled_from(["passed", "failed_check", "slower"]),
    st.floats(1, 1000, allow_nan=False),
    st.floats(0.1, 50, allow_nan=False),
)
_NOISE = st.lists(
    st.sampled_from(["agent_output_chunk", "tool_call", "usage_update", "rate_limit_update"]),
    max_size=12,
)


def _journal(
    ideas: list[bool], gates: list[_Gate], noise: list[str], direction: str = "max"
) -> list[bytes]:
    """A journal: the run starts, plays ``ideas`` (True for a probe) and ``gates``, with ``noise``."""
    lines = [_event(1, "run_started", 0, {"overview": {"direction": direction}})]
    for index, probe in enumerate(ideas):
        lines.append(
            _span(
                len(lines) + 1,
                1 + index,
                f"flow:{index}",
                _label("workstream", "accepted", {"probe": probe, "idea": f"idea {index}"}),
            )
        )
    for index, gate in enumerate(gates):
        lines.append(
            _span(
                len(lines) + 1,
                30 + index,
                f"op:{index}",
                _label(
                    "gate", gate.verdict, {"metric_value": gate.value, "vs_baseline": gate.ratio}
                ),
            )
        )
    for index, kind in enumerate(noise):
        lines.insert(1 + index % len(lines), _event(99, kind, 2, {"text": "span_ended"}))
    return lines


@given(
    ideas=st.lists(st.booleans(), max_size=8),
    gates=st.lists(_GATES, max_size=8),
    noise=_NOISE,
    direction=st.sampled_from(["max", "min"]),
)
@example(ideas=[], gates=[], noise=[], direction="max")
def test_a_digest_counts_the_ideas_and_takes_the_best_passed_gates_ratio(
    ideas: list[bool], gates: list[_Gate], noise: list[str], direction: str
) -> None:
    digest = digest_journal(_journal(ideas, gates, noise, direction))

    passed = [gate for gate in gates if gate.verdict == "passed"]
    pick = min if direction == "min" else max
    best = pick(passed, key=lambda gate: gate.value, default=None)
    assert digest.ideas == ideas.count(False)
    assert digest.best_ratio == (None if best is None else best.ratio)
    quiet = digest_journal(_journal(ideas, gates, [], direction))
    assert digest.model_copy(update={"duration_s": None}) == quiet.model_copy(
        update={"duration_s": None}
    )


@given(rounds=st.integers(1, 6), ideas=st.lists(st.booleans(), max_size=4), noise=_NOISE)
def test_rounds_without_a_trace_have_no_ideas_to_count_and_with_one_they_do(
    rounds: int, ideas: list[bool], noise: list[str]
) -> None:
    finished = [_event(50 + n, "round_finished", 10 + n) for n in range(rounds)]
    traced = _journal(ideas, [], noise)

    assert digest_journal([*_journal([], [], noise), *finished]).ideas is None
    assert digest_journal([*traced, *finished]).ideas == (ideas.count(False) if ideas else None)


def test_a_digest_reads_when_and_how_the_run_ended() -> None:
    lines = [
        _event(1, "server_started", 0),
        _event(
            2,
            "run_ending",
            61.5,
            {"reason": "adopted", "summary": "s", "resumable": False, "rounds_finished": 3},
        ),
        _event(3, "run_finished", 62),
    ]

    digest = digest_journal(lines)

    assert digest == RunDigest(
        ideas=0,
        best_ratio=None,
        duration_s=62,
        ending=RunEndingFact(
            outcome=RunEndOutcome.ADOPTED, summary="s", resumable=False, rounds_finished=3
        ),
        closing=RunClosing.FINISHED,
    )


def test_a_run_whose_server_died_has_no_ending_and_a_torn_last_line_is_skipped() -> None:
    lines = [_event(1, "server_started", 0), _event(2, "tool_call", 5), b'{"sequence":3,"ty']

    digest = digest_journal(lines)

    assert (digest.ending, digest.closing, digest.duration_s) == (None, None, 5)


def test_a_journal_with_no_events_has_no_duration() -> None:
    assert digest_journal(()) == RunDigest(
        ideas=0, best_ratio=None, duration_s=None, ending=None, closing=None
    )


def _write_journal(project: Path, lines: list[bytes]) -> Path:
    journal = Project.log_directory_for(project.resolve(), RUN_ID) / RUN_EVENTS_FILE
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes(b"".join(lines))
    return journal


def _list(project: Path, *, digest_runs: tuple[str, ...] | None) -> RunsListReply:
    threads = SimThreads()
    host = fake_host(threads, SimNetwork(threads))
    request = RunsListRequest(
        request_id="l", deadline_ms=5000, project=str(project), digest_runs=digest_runs
    )
    reply = answer(request, host, threads.event())
    assert isinstance(reply, RunsListReply)
    return reply


_ENDED = [
    _event(1, "server_started", 0),
    _event(2, "run_ending", 90, {"reason": "stopped", "resumable": True, "rounds_finished": 1}),
]


def test_a_run_ended_longer_ago_than_its_tombstone_lasts_still_says_how_it_ended(
    tmp_path: Path,
) -> None:
    """The registry keeps a tombstone a day; after that only the journal knows the ending."""
    project = project_with_run(tmp_path / "proj", RUN_ID)
    _write_journal(project, _ENDED)

    (run,) = _list(project, digest_runs=(RUN_ID,)).runs.runs

    assert run.phase is None
    assert isinstance(run.digest, RunDigest)
    assert run.digest.ending is not None
    assert run.digest.ending.outcome is RunEndOutcome.STOPPED


def test_only_the_runs_asked_for_carry_a_digest_and_an_unasked_reply_has_no_field(
    tmp_path: Path,
) -> None:
    project = project_with_run(tmp_path / "proj", RUN_ID)
    _write_journal(project, _ENDED)

    plain = _list(project, digest_runs=None)
    other = _list(project, digest_runs=("20261010-120000-22222222-other",))

    assert "digest" not in plain.model_dump(mode="json", exclude_none=False)["runs"]["runs"][0]
    assert other.runs.runs[0].digest is None


def test_asking_for_digests_needs_the_run_digest_feature() -> None:
    asked = RunsListRequest(request_id="l", deadline_ms=1, project="/p", digest_runs=("r",))
    plain = RunsListRequest(request_id="l", deadline_ms=1, project="/p")

    assert [feature.value for feature in request_features(asked)] == ["run_digest"]
    assert request_features(plain) == ()


def test_a_run_with_no_journal_has_no_digest_and_an_unreadable_one_says_so(
    tmp_path: Path,
) -> None:
    project = project_with_run(tmp_path / "proj", RUN_ID)
    projects = FileProjects(FakeGitRepositories().repository, SimThreads())
    root = project.resolve()

    assert projects.run_digest(root, RUN_ID) is None
    journal = Project.log_directory_for(root, RUN_ID) / RUN_EVENTS_FILE
    journal.mkdir(parents=True)  # a journal that exists but cannot be opened as a file
    unreadable = projects.run_digest(root, RUN_ID)
    assert isinstance(unreadable, RunDigestUnreadable)
    assert "\n" not in unreadable.message


def test_an_ended_runs_digest_is_kept_while_its_journal_is_unchanged_and_a_live_runs_is_not(
    tmp_path: Path,
) -> None:
    project = project_with_run(tmp_path / "proj", RUN_ID)
    projects = FileProjects(FakeGitRepositories().repository, SimThreads())
    root = project.resolve()
    journal = _write_journal(project, _ENDED)
    kept = projects.run_digest(root, RUN_ID)
    stamp = journal.stat()

    # Same size and time, other figures: a read that is not repeated cannot see them.
    journal.write_bytes(b"".join(_ENDED).replace(b'"stopped"', b'"adopted"'))
    os.utime(journal, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert projects.run_digest(root, RUN_ID) == kept

    live = [_event(1, "server_started", 0), _event(2, "tool_call", 5)]
    journal.write_bytes(b"".join(live))
    first = projects.run_digest(root, RUN_ID)
    journal.write_bytes(b"".join([*live, _event(3, "tool_call", 9)]))
    assert projects.run_digest(root, RUN_ID) != first


def test_a_recorded_run_whose_journal_is_not_here_is_absent_from_the_digests_not_empty(
    tmp_path: Path,
) -> None:
    """An empty digest read as a run that recorded nothing and stopped, with no ideas."""
    project = project_with_run(tmp_path / "proj", RUN_ID)

    (run,) = _list(project, digest_runs=(RUN_ID,)).runs.runs

    assert run.run_id == RUN_ID
    assert run.digest is None


def test_a_journal_from_before_the_trace_is_digested_without_ideas_through_the_helper(
    tmp_path: Path,
) -> None:
    fixture = json.loads(_LEGACY_FIXTURE.read_text())
    run_id = fixture["events"][0]["run_id"]
    project = project_with_run(tmp_path / "proj", run_id)
    journal = Project.log_directory_for(project.resolve(), run_id) / RUN_EVENTS_FILE
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes(b"".join(json.dumps(event).encode() + b"\n" for event in fixture["events"]))

    (run,) = _list(project, digest_runs=(run_id,)).runs.runs

    assert isinstance(run.digest, RunDigest)
    assert run.digest.ideas is None
    assert run.digest.closing is RunClosing.FINISHED
