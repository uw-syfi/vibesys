"""The Modal lifecycle translator: total, forward only, and never guessing."""

from __future__ import annotations

from hypothesis import given
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
)

_APP = "my-app"
_ENTRY = st.one_of(
    st.just({"app_name": _APP, "container_id": "c-1"}),
    st.just({"App Name": _APP, "Container ID": "c-2"}),
    st.just({"app_name": "other", "container_id": "c-3"}),
    st.just({"app_name": _APP}),
    st.just({"app_name": _APP, "container_id": ""}),
    st.just("not-a-dict"),
    st.just(None),
    st.dictionaries(st.text(max_size=5), st.text(max_size=5), max_size=3),
)
_LISTING = st.one_of(
    st.builds(ContainerListing, st.lists(_ENTRY, max_size=4).map(tuple)),
    st.builds(ListingFailed, st.text(max_size=20)),
)


def _rank(life: ContainerStarting | ContainerRunning | ContainerUnknown) -> int:
    if isinstance(life, ContainerRunning):
        return 1
    return 0 if isinstance(life, ContainerStarting) else -1


@given(listings=st.lists(_LISTING, max_size=20))
def test_a_listing_never_takes_a_known_container_life_backwards(
    listings: list[ContainerListing | ListingFailed],
) -> None:
    held: ContainerStarting | ContainerRunning | ContainerUnknown = ContainerUnknown()
    for listing in listings:
        after = observe_container(held, listing, _APP)
        assert _rank(after) >= _rank(held)
        if isinstance(held, ContainerRunning):
            assert after == held
        if not isinstance(held, ContainerUnknown):
            assert not isinstance(after, ContainerUnknown)
        held = after


@given(listing=_LISTING)
def test_only_a_matching_entry_with_an_id_is_a_running_container(
    listing: ContainerListing | ListingFailed,
) -> None:
    life = container_reading(listing, _APP)
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
