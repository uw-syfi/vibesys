"""The Modal lifecycle translator: total, forward only, and never guessing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

# test-isolation: the translator lives inside the standalone mounted helper, which is its only import path.
from vs_sandbox.modal_evaluator import (
    _EXEC_RC_MARKER,
    ContainerListing,
    ContainerRunning,
    ContainerStarting,
    ContainerUnknown,
    ExecEnd,
    ExecEnded,
    ExecUnknown,
    ListingFailed,
    _parse_exec_output,
    advance_container,
    container_reading,
    exec_life_of_marker,
    exit_status_of,
    health_ready,
    observe_container,
    started_since_deploy,
    unready_container_note,
)

_APP = "my-app"
_FLOOR = datetime(2026, 11, 7, 20, 59, tzinfo=UTC).timestamp()
_ZONES = st.sampled_from([UTC, timezone(timedelta(hours=-8)), timezone(timedelta(hours=5.5))])
_NO_INSTANT = st.sampled_from(
    [
        "Pending",
        "",
        None,
        _FLOOR + 60,
        "2026-11-07 21:00:00",
        "2026-11-07T21:00:00",
        "21:00:00+00:00",
        "tomorrow",
    ]
)
"""Start times that name no instant: not started yet, not text, no offset, or not a date-time."""


def _modal_start_time(seconds: float, zone: timezone) -> str:
    """``seconds`` since the epoch as ``modal container list --json`` prints a start time."""
    return datetime.fromtimestamp(seconds, zone).isoformat(sep=" ", timespec="seconds")


@st.composite
def _start_times(draw: st.DrawFn) -> tuple[object, float | None]:
    """A listed start time and the instant it names, None if it names none."""
    if draw(st.booleans()):
        return draw(_NO_INSTANT), None
    seconds = _FLOOR + draw(st.integers(-3600, 3600))
    return _modal_start_time(seconds, draw(_ZONES)), seconds


@st.composite
def _timed_entries(draw: st.DrawFn) -> tuple[dict[str, object], float | None]:
    """A listed container of the app or another, in either key style, and when it started."""
    start, instant = draw(_start_times())
    keys = draw(
        st.sampled_from(
            [("container_id", "app_name", "start_time"), ("Container ID", "App Name", "Start Time")]
        )
    )
    values = (draw(st.sampled_from(["ta-1", "ta-2", "ta-3"])), draw(st.sampled_from([_APP, "x"])))
    return dict(zip(keys, (*values, start), strict=True)), instant


_ENTRY = st.one_of(
    st.just({"app_name": _APP, "container_id": "c-1"}),
    st.just({"App Name": _APP, "Container ID": "c-2"}),
    st.just({"app_name": "other", "container_id": "c-3"}),
    st.just({"app_name": _APP}),
    st.just({"app_name": _APP, "container_id": ""}),
    st.just("not-a-dict"),
    st.just(None),
    st.dictionaries(st.text(max_size=5), st.text(max_size=5), max_size=3),
    _timed_entries().map(lambda timed: timed[0]),
)
_LISTING = st.one_of(
    st.builds(ContainerListing, st.lists(_ENTRY, max_size=4).map(tuple)),
    st.builds(ListingFailed, st.text(max_size=20)),
)
_STARTED_SINCE = st.one_of(st.none(), st.just(_FLOOR), st.floats(0, 4e9))


def _rank(life: ContainerStarting | ContainerRunning | ContainerUnknown) -> int:
    if isinstance(life, ContainerRunning):
        return 1
    return 0 if isinstance(life, ContainerStarting) else -1


@given(listings=st.lists(_LISTING, max_size=20), started_since=_STARTED_SINCE)
def test_a_listing_never_takes_a_known_container_life_backwards(
    listings: list[ContainerListing | ListingFailed], started_since: float | None
) -> None:
    held: ContainerStarting | ContainerRunning | ContainerUnknown = ContainerUnknown()
    for listing in listings:
        after = observe_container(held, listing, _APP, started_since=started_since)
        assert _rank(after) >= _rank(held)
        if isinstance(held, ContainerRunning):
            assert after == held
        if not isinstance(held, ContainerUnknown):
            assert not isinstance(after, ContainerUnknown)
        held = after


@given(listing=_LISTING)
def test_without_a_deploy_to_date_any_matching_entry_with_an_id_is_a_running_container(
    listing: ContainerListing | ListingFailed,
) -> None:
    life = container_reading(listing, _APP, started_since=None)
    if isinstance(listing, ListingFailed):
        assert life == ContainerUnknown(raw=listing.reason)
    else:
        matches = [
            entry
            for entry in listing.entries
            if isinstance(entry, dict)
            and (entry.get("app_name") or entry.get("App Name")) == _APP
            and (entry.get("container_id") or entry.get("Container ID"))
        ]
        assert isinstance(life, ContainerRunning) == bool(matches)
        assert life == ContainerStarting() or isinstance(life, ContainerRunning)


_OLD = {"Container ID": "ta-old", "App ID": "ap-1", "App Name": _APP}
_NEW = {"Container ID": "ta-new", "App ID": "ap-1", "App Name": _APP}


@pytest.mark.regression
@example(
    entries=[
        ({**_OLD, "Start Time": "2026-11-07 20:54:41+00:00"}, _FLOOR - 259),
        ({**_NEW, "Start Time": "Pending"}, None),
    ]
)
@example(entries=[({**_NEW, "Start Time": "2026-11-07 12:59:00-08:00"}, _FLOOR)])
@given(entries=st.lists(_timed_entries(), max_size=6))
def test_after_a_deploy_only_a_container_started_since_it_runs_the_deployed_version(
    entries: list[tuple[dict[str, object], float | None]],
) -> None:
    """Regression for #4097: the first listed container of the app could predate the deploy.

    Whatever the order, key style or offset, the chosen container started at or after the
    floor; one with no start time that names an instant is never chosen.
    """
    life = container_reading(
        ContainerListing(tuple(entry for entry, _ in entries)), _APP, started_since=_FLOOR
    )

    def listed(entry: dict[str, object], key: str, title: str) -> object:
        return entry.get(key, entry.get(title))

    of_app = [
        (listed(entry, "container_id", "Container ID"), instant)
        for entry, instant in entries
        if listed(entry, "app_name", "App Name") == _APP
    ]
    serving = [cid for cid, instant in of_app if instant is not None and instant >= _FLOOR]
    if serving:
        assert life == ContainerRunning(str(serving[0]))
    else:
        assert isinstance(life, ContainerStarting)
        assert len(life.passed_over) == len(of_app)
        assert all(
            str(cid) in shown for (cid, _), shown in zip(of_app, life.passed_over, strict=True)
        )


_DEPLOY_OUTPUT = st.lists(
    st.sampled_from(
        [
            "✓ Created objects.",
            "├── 🔨 Created web function serve => https://workspace--candidate.modal.run",
            "✓ App deployed in 43.702s! 🎉",
            "Using CPython 3.12.3 interpreter at: /usr/bin/python3",
            "View Deployment: https://modal.com/apps/workspace/main/deployed/candidate-app",
        ]
    ),
    max_size=6,
)
_SKIPPED = st.sampled_from(
    [
        "✓ Deployment skipped: no changes detected! ⏭️",
        "\x1b[32m✓\x1b[0m Deployment skipped: no\n│ changes detected! ⏭️",
    ]
)


@given(
    began=st.floats(1e9, 4e9),
    lines=_DEPLOY_OUTPUT,
    skipped=st.one_of(st.none(), _SKIPPED),
    at=st.integers(0, 6),
)
def test_a_deploy_dates_its_containers_unless_it_changed_nothing(
    began: float, lines: list[str], skipped: str | None, at: int
) -> None:
    if skipped is not None:
        lines.insert(at, skipped)
    floor = started_since_deploy(began, "\n".join(lines))
    if skipped is None:
        assert floor is not None
        assert 0 < began - floor <= 5, "a few seconds of skew, never later than the deploy began"
    else:
        assert floor is None


def test_a_finished_listing_after_running_is_stale() -> None:
    running = ContainerRunning("c-1")
    assert advance_container(running, ContainerStarting()) == running


@given(code=st.integers(0, 255))
def test_an_exit_code_marker_is_ended_with_that_code(code: int) -> None:
    life = exec_life_of_marker(str(code))
    assert life == ExecEnded(ExecEnd.OK if code == 0 else ExecEnd.NONZERO_EXIT, code)


@given(suffix=st.text(max_size=12))
def test_a_marker_that_is_not_a_plain_exit_code_is_unknown(suffix: str) -> None:
    """Regression: ``str.isdigit`` took ``"²"`` (then crashed in ``int``) and ``"٣"`` (as 3)."""
    life = exec_life_of_marker(suffix)
    plain = suffix.isascii() and suffix.isdigit() and len(suffix) <= 3 and int(suffix) <= 255
    assert isinstance(life, ExecEnded) == plain
    if not plain:
        assert life == ExecUnknown(raw=suffix)


@given(suffix=st.sampled_from(["²", "٣", "256", "-1", "1_0", "", "0x1", "9" * 5000]))
def test_known_bad_markers_never_raise_and_never_succeed(suffix: str) -> None:
    life, _files, _passthrough = _parse_exec_output(f"out\n{_EXEC_RC_MARKER}{suffix}\n")
    assert life == ExecUnknown(raw=suffix)
    assert exit_status_of(life, 0) != 0


@given(
    text=st.text(max_size=40),
    returncode=st.integers(0, 255),
    code=st.one_of(st.none(), st.integers(0, 255)),
)
def test_without_a_reported_exit_code_the_status_is_never_success(
    text: str, returncode: int, code: int | None
) -> None:
    body = text.replace(_EXEC_RC_MARKER, "")
    stdout = body if code is None else f"{body}\n{_EXEC_RC_MARKER}{code}\n"
    life, _files, _passthrough = _parse_exec_output(stdout)
    status = exit_status_of(life, returncode)
    if code is None:
        assert isinstance(life, ExecUnknown)
        assert status != 0
    else:
        assert status == code


@given(code=st.integers(0, 255))
def test_the_last_marker_is_the_reading(code: int) -> None:
    """The trusted script prints its marker last, so earlier marker lines are evaluator output."""
    stdout = f"{_EXEC_RC_MARKER}0\n{_EXEC_RC_MARKER}{code}\n"
    life, _files, _passthrough = _parse_exec_output(stdout)
    assert exit_status_of(life, 1) == code


@given(status=st.integers(0, 599))
def test_only_http_200_is_ready(status: int) -> None:
    assert health_ready(status) == (status == 200)


_NAME = st.text(alphabet="abc-0123", min_size=1, max_size=8)
_LIVES = st.one_of(
    st.builds(ContainerStarting, st.lists(_NAME, max_size=3).map(tuple)),
    st.builds(ContainerRunning, _NAME),
    st.builds(ContainerUnknown, st.one_of(st.none(), _NAME)),
)


@given(life=_LIVES, waited=st.floats(1.0, 86_400.0))
def test_only_a_readiness_timeout_with_no_container_listed_points_at_capacity(
    life: ContainerStarting | ContainerRunning | ContainerUnknown, waited: float
) -> None:
    """A gate points at GPU capacity exactly when no started container of the deployed version
    was listed, and names each listed container it passed over."""
    note = unready_container_note(life, _APP, waited)

    assert _APP in note
    assert ("capacity" in note) == isinstance(life, ContainerStarting)
    if isinstance(life, ContainerRunning):
        assert life.container_id in note
    if isinstance(life, ContainerStarting):
        assert all(container in note for container in life.passed_over)
    if isinstance(life, ContainerUnknown) and life.raw is not None:
        assert life.raw in note
