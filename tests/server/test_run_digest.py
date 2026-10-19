"""A run's digest: the figures a list row shows, folded from its journal and read through the helper."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
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
from server.host_run_dashboard import RunDashboard
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
class _Idea:
    """A workstream: a probe or not, of a hypothesis (``None``: an idea of its own)."""

    probe: bool
    hypothesis: int | None


_IDEAS = st.builds(_Idea, st.booleans(), st.none() | st.integers(0, 3))
_NOISE = st.lists(
    st.sampled_from(["agent_output_chunk", "tool_call", "usage_update", "rate_limit_update"]),
    max_size=12,
)


@dataclass(frozen=True)
class _Adoption:
    """An adoption span: its verdict (``None`` while adopting), revision and ratio."""

    verdict: str | None
    revision: str | None
    ratio: float


def _journal(
    ideas: list[_Idea],
    noise: list[str],
    direction: str = "max",
    adoption: _Adoption | None = None,
    gates: list[float] | None = None,
) -> list[bytes]:
    """A journal: the run starts and plays ``ideas`` (idea ``n`` ends on revision ``rev-n``).

    ``gates`` are passed gate readings, which are never the headline.
    """
    lines = [_event(1, "run_started", 0, {"overview": {"objective": {"direction": direction}}})]
    for index, idea in enumerate(ideas):
        attrs: dict[str, JsonValue] = {"probe": idea.probe, "revision": f"rev-{index}"}
        if idea.hypothesis is not None:
            attrs["hypothesis_id"] = f"h{idea.hypothesis}"
        span = {"id": f"flow:{index}", "label": _label("workstream", None, attrs)}
        lines.append(_event(len(lines) + 1, "span_started", 1 + index, {"span": span}))
        ended = _label("workstream", "accepted", attrs)
        lines.append(_span(len(lines) + 1, 1 + index, f"flow:{index}", ended))
    for index, value in enumerate(gates or []):
        gate = _label("gate", "passed", {"metric_value": value, "vs_baseline": value})
        lines.append(_span(len(lines) + 1, 30 + index, f"op:{index}", gate))
    if adoption is not None:
        attrs = {"candidate": adoption.revision, "vs_baseline": adoption.ratio}
        label = _label("adoption", adoption.verdict, attrs)
        lines.append(_span(len(lines) + 1, 50, "op:adopt", label))
    for index, kind in enumerate(noise):
        lines.insert(1 + index % len(lines), _event(99, kind, 2, {"text": "span_ended"}))
    return lines


def _idea_count(ideas: list[_Idea]) -> int:
    """The run page's count: an idea per hypothesis (per workstream with none), no probe."""
    return len(
        {
            idea.hypothesis if idea.hypothesis is not None else -1 - index
            for index, idea in enumerate(ideas)
            if not idea.probe
        }
    )


def _untimed(digest: RunDigest) -> RunDigest:
    """``digest`` without the figures that say when the journal's events were, which noise moves."""
    dashboard = digest.dashboard
    return digest.model_copy(
        update={
            "duration_s": None,
            "dashboard": None
            if dashboard is None
            else dashboard.model_copy(update={"last_event_at": None}),
        }
    )


@given(ideas=st.lists(_IDEAS, max_size=8), noise=_NOISE)
@example(ideas=[], noise=[])
def test_a_digest_counts_ideas_as_the_run_page_does(ideas: list[_Idea], noise: list[str]) -> None:
    digest = digest_journal(_journal(ideas, noise))

    assert digest.ideas == _idea_count(ideas)
    quiet = digest_journal(_journal(ideas, []))
    assert _untimed(digest) == _untimed(quiet)


@given(
    ideas=st.lists(_IDEAS, min_size=1, max_size=6),
    pick=st.integers(0, 5),
    adopting=st.builds(
        _Adoption,
        st.sampled_from(["adopted", "not_adopted", None]),
        st.none(),
        st.floats(0.1, 50, allow_nan=False),
    ),
    gates=st.lists(st.floats(1, 1000, allow_nan=False), max_size=6),
    direction=st.sampled_from(["max", "min"]),
)
def test_a_digests_ratio_is_the_adopted_results_whatever_the_gates_measured(
    ideas: list[_Idea],
    pick: int,
    adopting: _Adoption,
    gates: list[float],
    direction: str,
) -> None:
    index = pick % len(ideas)
    adoption = replace(adopting, revision=f"rev-{index}")
    verdict, ratio = adoption.verdict, adoption.ratio

    digest = digest_journal(_journal(ideas, [], direction, adoption, gates))

    headlined = not ideas[index].probe and verdict != "not_adopted"
    improvement = ratio if direction == "max" else 1 / ratio
    assert digest.adopted_ratio == (improvement if headlined else None)


@given(gates=st.lists(st.floats(1, 1000, allow_nan=False), min_size=1, max_size=6))
def test_a_run_that_adopted_nothing_has_no_ratio_however_well_a_gate_measured(
    gates: list[float],
) -> None:
    ideas = [_Idea(probe=False, hypothesis=None)]
    assert digest_journal(_journal(ideas, [], gates=gates)).adopted_ratio is None
    input_stands = _Adoption("adopted", None, 1.0)
    assert (
        digest_journal(_journal(ideas, [], adoption=input_stands, gates=gates)).adopted_ratio
        is None
    )


@dataclass(frozen=True)
class _Measured:
    """An idea that was measured at ``value`` against an input of 100, and passed or failed."""

    passed: bool
    value: float


_MEASURED = st.builds(_Measured, st.booleans(), st.floats(20, 500, allow_nan=False))


def _measured_journal(
    ideas: list[_Measured], noise: list[str], direction: str, adoption: _Adoption | None = None
) -> list[bytes]:
    """A journal in which idea ``n`` is a workstream with a gate under it that read its value."""
    lines = [_event(1, "run_started", 0, {"overview": {"objective": {"direction": direction}}})]
    for index, idea in enumerate(ideas):
        attrs: dict[str, JsonValue] = {"hypothesis_id": f"h{index}", "revision": f"rev-{index}"}
        started = {"id": f"flow:{index}", "label": _label("workstream", None, attrs)}
        lines.append(_event(len(lines) + 1, "span_started", 1 + index, {"span": started}))
        gate: dict[str, JsonValue] = {"metric_value": idea.value, "vs_baseline": idea.value / 100}
        span = {
            "id": f"op:{index}",
            "parent": f"flow:{index}",
            "label": _label("gate", "passed", gate),
        }
        lines.append(_event(len(lines) + 1, "span_ended", 1 + index, {"span": span}))
        verdict = "passed" if idea.passed else "failed"
        ended = _label("workstream", verdict, attrs)
        lines.append(_span(len(lines) + 1, 1 + index, f"flow:{index}", ended))
    if adoption is not None:
        attrs = {"candidate": adoption.revision, "vs_baseline": adoption.ratio}
        label = _label("adoption", adoption.verdict, attrs)
        lines.append(_span(len(lines) + 1, 50, "op:adopt", label))
    for index, kind in enumerate(noise):
        lines.insert(1 + index % len(lines), _event(99, kind, 2, {"text": "span_ended"}))
    return lines


@given(
    ideas=st.lists(_MEASURED, max_size=6),
    direction=st.sampled_from(["max", "min"]),
    noise=_NOISE,
)
def test_a_run_that_is_still_working_headlines_its_best_passed_idea_that_beat_the_input(
    ideas: list[_Measured], direction: str, noise: list[str]
) -> None:
    digest = digest_journal(_measured_journal(ideas, noise, direction))

    beating = [
        idea.value
        for idea in ideas
        if idea.passed and (idea.value > 100 if direction == "max" else idea.value < 100)
    ]
    best = (
        None if not beating else (max(beating) / 100 if direction == "max" else 100 / min(beating))
    )
    assert digest.best_so_far_ratio == pytest.approx(best)
    assert digest.adopted_ratio is None


@given(
    ideas=st.lists(_MEASURED, min_size=1, max_size=4),
    verdict=st.sampled_from(["adopted", "not_adopted", None]),
)
def test_a_run_that_has_opened_its_adoption_has_no_best_so_far(
    ideas: list[_Measured], verdict: str | None
) -> None:
    adoption = _Adoption(verdict, "rev-0", 1.5)

    digest = digest_journal(_measured_journal(ideas, [], "max", adoption))

    assert digest.best_so_far_ratio is None


def test_an_idea_still_running_or_whose_gate_reads_for_a_failed_attempt_is_not_the_best() -> None:
    def workstream(sequence: int, kind: str, verdict: str | None) -> bytes:
        label = _label("workstream", verdict, {"hypothesis_id": "h"})
        return _event(sequence, kind, sequence, {"span": {"id": "flow:1", "label": label}})

    gate_label = _label("gate", "passed", {"metric_value": 150.0, "vs_baseline": 1.5})
    gate: dict[str, JsonValue] = {"id": "op:1", "parent": "flow:1", "label": gate_label}
    measured = _event(3, "span_ended", 3, {"span": gate})
    running = [_event(1, "run_started", 0), workstream(2, "span_started", None), measured]

    assert digest_journal(running).best_so_far_ratio is None
    failed = [*running, workstream(4, "span_ended", "failed")]
    assert digest_journal(failed).best_so_far_ratio is None
    passed = [*running, workstream(4, "span_ended", "passed")]
    assert digest_journal(passed).best_so_far_ratio == 1.5


@given(rounds=st.integers(1, 6), ideas=st.lists(_IDEAS, max_size=4), noise=_NOISE)
def test_rounds_without_a_trace_have_no_ideas_to_count_and_with_one_they_do(
    rounds: int, ideas: list[_Idea], noise: list[str]
) -> None:
    finished = [_event(50 + n, "round_finished", 10 + n) for n in range(rounds)]
    traced = _journal(ideas, noise)

    assert digest_journal([*_journal([], noise), *finished]).ideas is None
    assert digest_journal([*traced, *finished]).ideas == (_idea_count(ideas) if ideas else None)


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

    assert digest.dashboard is not None
    assert digest.dashboard.points == ()
    assert digest.model_copy(update={"dashboard": None}) == RunDigest(
        ideas=0,
        adopted_ratio=None,
        best_so_far_ratio=None,
        duration_s=62,
        ending=RunEndingFact(
            outcome=RunEndOutcome.ADOPTED, summary="s", resumable=False, rounds_finished=3
        ),
        closing=RunClosing.FINISHED,
    )


def test_a_digest_reads_that_an_adoption_the_deadline_ended_was_never_compared() -> None:
    """H1: a run whose tombstone is gone still says, from its journal, it adopted uncompared."""
    lines = [
        _event(1, "server_started", 0),
        _event(
            2,
            "run_ending",
            61.5,
            {
                "reason": "deadline",
                "summary": "deadline: adopted: c1",
                "resumable": False,
                "rounds_finished": 3,
                "uncompared": True,
            },
        ),
    ]

    digest = digest_journal(lines)

    assert digest.ending is not None
    assert digest.ending.outcome is RunEndOutcome.DEADLINE
    assert digest.ending.uncompared


def test_a_run_whose_server_died_has_no_ending_and_a_torn_last_line_is_skipped() -> None:
    lines = [_event(1, "server_started", 0), _event(2, "tool_call", 5), b'{"sequence":3,"ty']

    digest = digest_journal(lines)

    assert (digest.ending, digest.closing, digest.duration_s) == (None, None, 5)


def test_a_journal_with_no_events_has_no_duration() -> None:
    assert digest_journal(()) == RunDigest(
        ideas=0,
        adopted_ratio=None,
        best_so_far_ratio=None,
        duration_s=None,
        ending=None,
        closing=None,
        dashboard=RunDashboard(points=(), points_omitted=0, failed_streak=0, last_event_at=None),
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
