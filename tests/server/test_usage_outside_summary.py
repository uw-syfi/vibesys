"""The reply counts a person's CLI use beside the runs, and never the runs' own sessions twice.

A run and a person used the same provider on the same host in the same hour. The run's journal
names its conversation, so the transcript of that conversation is the run's and is left out of
"outside runs"; the person's session is not. The files are real ones in scratch directories, the
journal is the run's, and the reply is the one the registered read returns.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.server.run_summary_support import EXECUTION_IDS, RUN_ID
from tests.server.transcript_support import START, Request, line, session_file

from server.events import (
    AgentExecutionActivityData,
    AgentExecutionStartedData,
    EventType,
    RunEvent,
    TurnUsageData,
)
from server.host_api import RUN_EVENTS_FILE
from server.run_summary_cache import summarize_journal
from server.usage_scan import stored_pricing, usage_directory, usage_summary
from server.usage_transcripts import SCAN_FILE
from vs_project.api import Project
from vs_proto.api import Timestamp, UsageSpeed, host_payloads, usage_state, validate
from vs_sim.api.testing import ManualClock

if TYPE_CHECKING:
    from server.run_summary import Summarized

NOW = START + timedelta(hours=2)


class _Projects:
    def run_summary(self, project: Path, run_id: str) -> Summarized:
        return summarize_journal(Project.log_directory_for(project, run_id) / RUN_EVENTS_FILE)

    def run_digest(self, project_root: Path, run_id: str) -> None:
        del project_root, run_id


def _events() -> bytes:
    started = RunEvent(
        sequence=1,
        run_id=RUN_ID,
        timestamp=START,
        type=EventType.AGENT_EXECUTION_STARTED,
        data=AgentExecutionStartedData(
            stage="implementer",
            activity=AgentExecutionActivityData(mode="thinking", summary="working"),
            provider="claude",
            model="claude-opus-5-5",
        ),
        execution_id=EXECUTION_IDS[1],
        agent_kind="implementer",
    )
    turn = RunEvent(
        sequence=2,
        run_id=RUN_ID,
        timestamp=START + timedelta(minutes=3),
        type=EventType.TURN_USAGE_RECORDED,
        data=TurnUsageData(
            input_tokens=1000,
            output_tokens=100,
            total_cost_usd=0.5,
            cost_basis="price_table",
            provider_session_id="sess-run",
        ),
        execution_id=EXECUTION_IDS[1],
    )
    return b"".join(event.model_dump_json().encode() + b"\n" for event in (started, turn))


def _request(edges: list[datetime], project: Path) -> host_payloads.UsageSummaryRequest:
    request = host_payloads.UsageSummaryRequest(
        request_id="r",
        projects=[str(project)],
        edges=[Timestamp.from_datetime(edge) for edge in edges],
        now=Timestamp.from_datetime(NOW),
    )
    validate(request)
    return request


def test_a_persons_session_is_outside_and_the_runs_own_is_not_counted_twice() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        project, home = root / "project", root / "home"
        project.mkdir()
        journal = Project.log_directory_for(project, RUN_ID) / RUN_EVENTS_FILE
        journal.parent.mkdir(parents=True)
        journal.write_bytes(_events())
        Project.publish_run_for(project, RUN_ID)
        runs = Request("sess-run", 3, "claude-opus-5-5", "/work/alpha", 1000, 0, 100)
        person = Request("sess-person", 4, "claude-opus-5-5", "/work/alpha", 700, 0, 70)
        for index, request in enumerate((runs, person)):
            session_file(home, request.session).write_bytes(line(request, index))
        edges = [START - timedelta(hours=1), START + timedelta(hours=3)]

        reply = usage_summary(
            _request(edges, project),
            _Projects(),
            [project],
            stored_pricing(usage_directory(), now=NOW),
            {"HOME": str(home)},
            clock=ManualClock(NOW.timestamp()),
        )

        validate(reply)
        by_origin = {
            origin: sum(
                sum(series.uncached) + sum(series.output)
                for series in reply.series
                if series.key and series.key.origin == origin
            )
            for origin in (usage_state.UsageOrigin.RUN, usage_state.UsageOrigin.OUTSIDE_RUNS)
        }
        assert by_origin == {
            usage_state.UsageOrigin.RUN: 1100,
            usage_state.UsageOrigin.OUTSIDE_RUNS: 770,
        }
        assert reply.pending_runs == reply.pending_files == 0
        transcripts = [
            each for each in reply.sources if each.kind == usage_state.UsageSourceKind.TRANSCRIPTS
        ]
        claude = next(each for each in transcripts if each.provider == "claude")
        assert (claude.excluded_exact, claude.distinct_sessions) == (1, 1)
        assert sum(count.outside_sessions for count in reply.counts) == 1
        assert sum(count.runs for count in reply.counts) == 1


def test_the_outside_series_carry_the_index_of_the_source_they_were_read_from() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        project, home = root / "project", root / "home"
        project.mkdir()
        session_file(home, "sess-person").write_bytes(
            line(Request("sess-person", 4, "claude-opus-5-5", "/work/alpha", 700, 0, 70), 0)
        )
        reply = usage_summary(
            _request([START - timedelta(hours=1), START + timedelta(hours=3)], project),
            _Projects(),
            [project],
            stored_pricing(usage_directory(), now=NOW),
            {"HOME": str(home)},
            clock=ManualClock(NOW.timestamp()),
        )
        outside = [
            series
            for series in reply.series
            if series.key and series.key.origin == usage_state.UsageOrigin.OUTSIDE_RUNS
        ]
        assert outside
        for series in outside:
            assert series.key is not None
            source = reply.sources[series.key.usage_source]
            assert source.kind == usage_state.UsageSourceKind.TRANSCRIPTS
            assert source.provider == "claude"
            assert series.key.speed == UsageSpeed.STANDARD


def test_a_damaged_price_book_is_said_in_the_reply_and_the_figures_still_come() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        project, home = root / "project", root / "home"
        project.mkdir()
        session_file(home, "sess-person").write_bytes(
            line(Request("sess-person", 4, "claude-opus-5-5", "/work/alpha", 700, 0, 70), 0)
        )
        directory = usage_directory()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "prices.pb").write_bytes(b"\xff\xfe damaged")

        reply = usage_summary(
            _request([START - timedelta(hours=1), START + timedelta(hours=3)], project),
            _Projects(),
            [project],
            stored_pricing(directory, now=NOW),
            {"HOME": str(home)},
            clock=ManualClock(NOW.timestamp()),
        )

        validate(reply)
        assert reply.prices_status is usage_state.UsagePricesStatus.UNREADABLE
        assert reply.prices == usage_state.UsagePriceBook()
        assert reply.series
        assert (directory / "prices.pb").read_bytes() == b"\xff\xfe damaged"


def _cached_rows() -> int:
    """How many rows of a person's use the host's transcript cache holds."""
    cache = usage_state.TranscriptScanCache.from_binary(
        (usage_directory() / SCAN_FILE).read_bytes()
    )
    return sum(len(each.rows) for root in cache.roots for each in root.files)


@settings(max_examples=12, deadline=None)
@given(days=st.integers(min_value=-4000, max_value=4000))
def test_a_wrong_clock_in_the_request_does_not_erase_history_the_host_holds(days: int) -> None:
    """A client whose clock is years off cannot make the host forget transcripts it cannot reread.

    The host forgets rows for age by its own clock. The request's ``now`` says only when the
    figures are as of; here it is anywhere within about eleven years of the data.
    """
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        project, home = root / "project", root / "home"
        project.mkdir()
        session_file(home, "sess-person").write_bytes(
            line(Request("sess-person", 4, "claude-opus-5-5", "/work/alpha", 700, 0, 70), 0)
        )
        (usage_directory() / SCAN_FILE).unlink(missing_ok=True)
        edges = [START - timedelta(hours=1), START + timedelta(hours=3)]

        def ask(client_now: datetime) -> host_payloads.UsageSummaryResponse:
            request = _request(edges, project)
            request.now = Timestamp.from_datetime(client_now)
            return usage_summary(
                request,
                _Projects(),
                [project],
                stored_pricing(usage_directory(), now=NOW),
                {"HOME": str(home)},
                clock=ManualClock(NOW.timestamp()),
            )

        honest = ask(NOW)
        held = _cached_rows()
        assert held > 0
        wrong = ask(NOW + timedelta(days=days))

        assert _cached_rows() == held
        assert [list(each.uncached) for each in wrong.series] == [
            list(each.uncached) for each in honest.series
        ]


def test_a_request_that_names_no_project_counts_every_run_the_host_indexed() -> None:
    """No list to cut: the runs are the host's own index, and the runs' sessions are still theirs."""
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        project, home = root / "project", root / "home"
        project.mkdir()
        journal = Project.log_directory_for(project, RUN_ID) / RUN_EVENTS_FILE
        journal.parent.mkdir(parents=True)
        journal.write_bytes(_events())
        Project.publish_run_for(project, RUN_ID)
        for index, request in enumerate(
            (
                Request("sess-run", 3, "claude-opus-5-5", "/work/alpha", 1000, 0, 100),
                Request("sess-person", 4, "claude-opus-5-5", "/work/alpha", 700, 0, 70),
            )
        ):
            session_file(home, request.session).write_bytes(line(request, index))
        request = _request([START - timedelta(hours=1), START + timedelta(hours=3)], project)
        del request.projects[:]
        validate(request)

        reply = usage_summary(
            request,
            _Projects(),
            [],
            stored_pricing(usage_directory(), now=NOW),
            {"HOME": str(home)},
            clock=ManualClock(NOW.timestamp()),
        )

        validate(reply)
        tokens = {
            origin: sum(
                sum(series.uncached) + sum(series.output)
                for series in reply.series
                if series.key and series.key.origin == origin
            )
            for origin in (usage_state.UsageOrigin.RUN, usage_state.UsageOrigin.OUTSIDE_RUNS)
        }
        assert tokens == {
            usage_state.UsageOrigin.RUN: 1100,
            usage_state.UsageOrigin.OUTSIDE_RUNS: 770,
        }
        assert [each.run_id for each in reply.runs] == [RUN_ID]
