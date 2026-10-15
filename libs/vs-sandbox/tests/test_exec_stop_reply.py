"""The stop script's reply line parses back to what the script reported."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

# test-isolation: the stop script's reply protocol is internal to the Docker sandbox; this pins it directly.
from vs_sandbox.docker_cli import ExecStopEvidence, ExecStopReply, parse_exec_stop_reply

_ticks = st.integers(0, 2**62).map(str)


def _line(survivors: int | None, evidence: ExecStopEvidence) -> str:
    """Format a reply the way the script's ``echo "$r ${T:--}$S"`` does."""
    count = "unknown" if survivors is None else str(survivors)
    sessions = "".join(f"{session} " for session in evidence.sessions)
    return f"{count} {evidence.since or '-'} {sessions}\n"


@given(
    survivors=st.one_of(st.none(), st.integers(0, 10_000)),
    sessions=st.lists(_ticks, max_size=4, unique=True).map(tuple),
    since=st.one_of(st.none(), _ticks),
)
def test_a_reply_parses_back_to_its_count_and_evidence(
    survivors: int | None, sessions: tuple[str, ...], since: str | None
) -> None:
    evidence = ExecStopEvidence(sessions=sessions, since=since)
    assert parse_exec_stop_reply(_line(survivors, evidence)) == ExecStopReply(survivors, evidence)


@given(evidence=st.builds(ExecStopEvidence, st.lists(_ticks, max_size=4).map(tuple)))
def test_evidence_survives_the_trip_through_the_script_operands(
    evidence: ExecStopEvidence,
) -> None:
    sessions, since = evidence.operands()
    assert tuple(sessions.split()) == evidence.sessions
    assert since == (evidence.since or "")


@given(
    line=st.one_of(
        st.just(""),
        st.just("3"),
        st.just("-1 -"),
        st.just("gone -"),
        st.just("2 later"),
        st.just("2 - x"),
        st.text(alphabet="abc ", min_size=1).filter(lambda text: text.split()[:1] != []),
    )
)
def test_a_reply_outside_the_protocol_is_rejected(line: str) -> None:
    assert parse_exec_stop_reply(line) is None
