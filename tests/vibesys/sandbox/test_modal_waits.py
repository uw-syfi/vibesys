"""The Modal helper's two wait loops keep to their deadline, on a virtual clock.

``wait_for_health`` polls a deployment's ``/health`` and ``find_app_container`` polls
Modal's container list. Each takes the clock it reads and sleeps on, and the probe it
runs, so these tests run them in virtual seconds against scripted probes. Whatever the
probes answer and however long each takes, a loop never pauses or probes past its
deadline, keeps probing until that deadline, and returns as soon as a probe succeeds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_sandbox.modal_evaluator import (
    ContainerListing,
    ListingFailed,
    find_app_container,
    wait_for_health,
)
from vs_sim.api.testing import ManualClock, examples

if TYPE_CHECKING:
    from collections.abc import Callable

URL = "https://workspace--candidate.modal.run"
APP = "candidate-app"
_RUNNING = ContainerListing(({"app_name": APP, "container_id": "ta-1"},))
_STARTING = ContainerListing(({"app_name": "another-app", "container_id": "ta-2"},))
_ROUNDING = 1e-9
"""How far a virtual time may pass a deadline from float rounding alone.

The loops pause for ``deadline - now``, and ``now + (deadline - now)`` can land one ulp
past ``deadline`` (2.7284237543152616 + 4.857555907057096 is 7.585979661372358).
"""


class AdvancingClock:
    """A manual clock whose blocking sleep moves it forward, recording each pause."""

    def __init__(self) -> None:
        """Start at zero with no pauses."""
        self._clock = ManualClock(0.0)
        self.sleeps: list[float] = []

    def now(self) -> float:
        """The current time."""
        return self._clock.now()

    def advance(self, seconds: float) -> None:
        """Move the clock forward without pausing."""
        self._clock.advance(seconds)

    def sleep(self, seconds: float) -> None:
        """Pause ``seconds`` manual seconds."""
        self.sleeps.append(seconds)
        self._clock.advance(seconds)


type HealthAnswer = int | OSError
"""A status ``/health`` returns, or the error the probe raises."""

_ANSWERS = st.one_of(st.sampled_from([200, 404, 502, 503]), st.just(OSError("connection refused")))


def _health_probe(
    clock: AdvancingClock, answers: list[HealthAnswer], took: list[float]
) -> tuple[Callable[[str, float], int], list[float]]:
    """A probe that answers from ``answers`` (then 503), taking up to its timeout each time."""
    timeouts: list[float] = []

    def probe(url: str, timeout: float) -> int:
        assert url == f"{URL}/health"
        index = len(timeouts)
        timeouts.append(timeout)
        clock.advance(min(took[index % len(took)], timeout))
        answer = answers[index] if index < len(answers) else 503
        if isinstance(answer, OSError):
            raise answer
        return answer

    return probe, timeouts


@settings(max_examples=examples(pr=40, nightly=1000), deadline=None)
@given(
    timeout=st.floats(0.0, 120.0),
    answers=st.lists(_ANSWERS, max_size=40),
    took=st.lists(st.floats(0.0, 15.0), min_size=1, max_size=5),
)
def test_wait_for_health_keeps_to_its_deadline(
    timeout: float, answers: list[HealthAnswer], took: list[float]
) -> None:
    clock = AdvancingClock()
    probe, timeouts = _health_probe(clock, answers, took)

    try:
        wait_for_health(URL, timeout_seconds=timeout, clock=clock, probe=probe)
    except TimeoutError:
        assert 200 not in answers[: len(timeouts)]
        assert clock.now() == pytest.approx(timeout) or (timeout == 0 and not timeouts)
    else:
        assert answers[len(timeouts) - 1] == 200, "it returns at the first ready answer"
        assert 200 not in answers[: len(timeouts) - 1]
        assert clock.now() <= timeout + _ROUNDING
    assert clock.now() <= max(timeout, 0.0) + _ROUNDING, "nothing ran past the deadline"
    assert all(0 < limit <= 10.0 for limit in timeouts)
    assert all(0 < pause <= 2.0 for pause in clock.sleeps)


def test_wait_for_health_reports_the_last_error() -> None:
    clock = AdvancingClock()
    probe, _ = _health_probe(clock, [OSError("refused"), 502], [0.0])

    with pytest.raises(TimeoutError, match="did not become ready: HTTP 503"):
        wait_for_health(URL, timeout_seconds=7, clock=clock, probe=probe)
    assert clock.sleeps == [2.0, 2.0, 2.0, 1.0]


def test_wait_for_health_rejects_untrusted_urls_before_any_probe() -> None:
    clock = AdvancingClock()
    probe, timeouts = _health_probe(clock, [], [0.0])

    with pytest.raises(ValueError, match="invalid web URL"):
        wait_for_health("https://evil.example", timeout_seconds=1, clock=clock, probe=probe)
    assert timeouts == []


_LISTINGS = st.sampled_from([_STARTING, ListingFailed("modal exited 1"), ContainerListing(())])


@settings(max_examples=examples(pr=40, nightly=1000), deadline=None)
@example(timeout=7.585979661372357, listings=[], running_after=None, took=[2.7284237543152616])
@given(
    timeout=st.floats(0.0, 300.0),
    listings=st.lists(_LISTINGS, max_size=80),
    running_after=st.one_of(st.none(), st.integers(0, 80)),
    took=st.lists(st.floats(0.0, 20.0), min_size=1, max_size=5),
)
def test_find_app_container_keeps_to_its_deadline(
    timeout: float, listings: list[object], running_after: int | None, took: list[float]
) -> None:
    clock = AdvancingClock()
    listed_at: list[float] = []
    warmed: list[str] = []

    def list_containers(workspace: str) -> ContainerListing | ListingFailed:
        assert workspace == "/workspace"
        index = len(listed_at)
        listed_at.append(clock.now())
        clock.advance(took[index % len(took)])
        if running_after is not None and index >= running_after:
            return _RUNNING
        listing = listings[index] if index < len(listings) else _STARTING
        assert isinstance(listing, ContainerListing | ListingFailed)
        return listing

    def warm(base_url: str) -> bool:
        warmed.append(base_url)
        return False

    try:
        found = find_app_container(
            APP,
            workspace="/workspace",
            base_url=URL,
            timeout_seconds=timeout,
            clock=clock,
            list_containers=list_containers,
            warm=warm,
        )
    except TimeoutError:
        assert running_after is None or running_after >= len(listed_at)
        assert clock.now() >= timeout, "it lists until the deadline"
    else:
        assert found == "ta-1"
        assert running_after == len(listed_at) - 1, "it returns at the first running listing"
    assert all(at <= timeout + _ROUNDING for at in listed_at), "no listing starts past the deadline"
    assert all(0 < pause <= 5.0 for pause in clock.sleeps)
    assert warmed == [URL] * len(clock.sleeps)
