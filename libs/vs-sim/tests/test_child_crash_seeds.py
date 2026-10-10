"""Child isolation, restart loops, crash-point selection, seeds and random streams."""

from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sim.api import SeededRandom, SystemRandomSource, derive_seed
from vs_sim.api.testing import (
    ChildDiedError,
    RestartLimitError,
    VirtualClock,
    after_crash,
    evenly_spaced,
    first_of_each_kind,
    random_for,
    replay_hint,
    restart_until_done,
    run_in_child,
    run_virtual,
    seed_for_test,
)


def _return_pid() -> int:
    return os.getpid()


def _change_process_state() -> str:
    os.environ["VS_SIM_CHILD_ONLY"] = "set"
    os.chdir("/")
    signal.signal(signal.SIGUSR2, lambda *_: None)
    signal.setitimer(signal.ITIMER_REAL, 1000.0)
    return os.environ["VS_SIM_CHILD_ONLY"]


def _raise_key_error() -> None:
    message = "raised in the child"
    raise KeyError(message)


def _kill_self() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def _exit_early() -> None:
    os._exit(3)


def _return_unpicklable() -> object:
    return lambda: None


def test_the_child_is_another_process() -> None:
    assert run_in_child(_return_pid) != os.getpid()


def test_process_wide_state_changed_in_the_child_does_not_reach_the_test_process() -> None:
    cwd = Path.cwd()
    handler = signal.getsignal(signal.SIGUSR2)
    timer = signal.getitimer(signal.ITIMER_REAL)

    assert run_in_child(_change_process_state) == "set"

    assert "VS_SIM_CHILD_ONLY" not in os.environ
    assert Path.cwd() == cwd
    assert signal.getsignal(signal.SIGUSR2) == handler
    assert signal.getitimer(signal.ITIMER_REAL)[0] <= timer[0] or timer[0] == 0.0


def test_an_exception_in_the_child_is_raised_here_with_the_childs_traceback() -> None:
    with pytest.raises(KeyError, match="raised in the child") as raised:
        run_in_child(_raise_key_error)
    assert any("_raise_key_error" in note for note in raised.value.__notes__)


def test_a_child_killed_by_a_signal_is_reported_by_name() -> None:
    with pytest.raises(ChildDiedError, match="SIGKILL"):
        run_in_child(_kill_self)


def test_a_child_that_exits_without_a_result_is_reported_with_its_status() -> None:
    with pytest.raises(ChildDiedError, match="status 3"):
        run_in_child(_exit_early)


def test_a_result_that_cannot_be_pickled_is_reported() -> None:
    with pytest.raises(ChildDiedError, match="pickled"):
        run_in_child(_return_unpicklable)


class HostDiedError(Exception):
    pass


@given(st.integers(min_value=0, max_value=6), st.integers(min_value=0, max_value=6))
def test_restart_until_done_restarts_once_per_death(deaths: int, spare: int) -> None:
    started: list[int] = []

    async def boot(generation: int) -> str:
        started.append(generation)
        if generation < deaths:
            raise HostDiedError
        return f"done after {generation}"

    outcome = run_virtual(
        VirtualClock(),
        restart_until_done(boot, crashed=HostDiedError, max_crashes=deaths + spare),
    )
    assert (outcome.result, outcome.crashes) == (f"done after {deaths}", deaths)
    assert started == list(range(deaths + 1))


def test_a_host_that_keeps_dying_past_the_plan_is_an_error() -> None:
    async def boot(_generation: int) -> None:
        raise HostDiedError

    with pytest.raises(RestartLimitError, match="2 deaths"):
        run_virtual(VirtualClock(), restart_until_done(boot, crashed=HostDiedError, max_crashes=2))


def test_an_error_that_is_not_a_death_ends_the_run() -> None:
    async def boot(_generation: int) -> None:
        message = "a bug, not a crash"
        raise ValueError(message)

    with pytest.raises(ValueError, match="a bug"):
        run_virtual(VirtualClock(), restart_until_done(boot, crashed=HostDiedError, max_crashes=2))


@given(st.lists(st.sampled_from("abcde"), max_size=30))
def test_first_of_each_kind_points_at_the_first_occurrence(kinds: list[str]) -> None:
    firsts = first_of_each_kind(kinds)
    assert set(firsts) == set(kinds)
    assert all(kinds[index] == kind and kind not in kinds[:index] for kind, index in firsts.items())


@given(st.integers(min_value=0, max_value=200), st.integers(min_value=0, max_value=50))
def test_evenly_spaced_picks_distinct_indexes_including_both_ends(count: int, samples: int) -> None:
    picked = evenly_spaced(count, samples)
    assert picked == sorted(set(picked))
    assert all(0 <= index < count for index in picked)
    assert len(picked) <= max(samples, 0)
    if count and samples >= 2:
        assert picked[0] == 0
        assert picked[-1] == count - 1
    if samples >= count:
        assert picked == list(range(count))


@given(st.lists(st.integers(), min_size=1, max_size=20), st.data())
def test_after_crash_is_what_follows_the_crash_point(
    recorded: list[int], data: st.DataObject
) -> None:
    index = data.draw(st.integers(min_value=0, max_value=len(recorded) - 1))
    assert list(after_crash(recorded, index)) == recorded[index + 1 :]


@given(st.text(max_size=30), st.integers(min_value=0, max_value=2**32))
def test_a_tests_seed_is_a_pure_function_of_its_id_unless_overridden(
    test_id: str, override: int
) -> None:
    assert seed_for_test(test_id) == seed_for_test(test_id)
    assert seed_for_test(test_id, override) == override
    assert replay_hint(override) == f"--sim-seed={override}"


@given(st.integers(min_value=0, max_value=2**32), st.text(max_size=10), st.text(max_size=10))
def test_forked_streams_are_independent_of_each_other_and_of_draws_on_the_parent(
    seed: int, label_a: str, label_b: str
) -> None:
    parent = SeededRandom(seed)
    first = parent.fork(label_a)
    expected = [first.random() for _ in range(4)]
    parent.random()  # drawing from the parent or another fork does not move a fork
    parent.fork(label_b).random()
    replay = SeededRandom(seed).fork(label_a)
    assert [replay.random() for _ in range(4)] == expected
    assert random_for(seed, label_a).random() == SeededRandom(derive_seed(seed, label_a)).random()


@given(st.integers(min_value=0, max_value=2**32), st.lists(st.integers(), min_size=1, max_size=20))
def test_a_seeded_source_replays_its_draws(seed: int, items: list[int]) -> None:
    def draws(source: SeededRandom) -> tuple[object, ...]:
        shuffled = list(items)
        source.shuffle(shuffled)
        return (source.random(), source.randint(0, 9), source.choice(items), tuple(shuffled))

    assert draws(SeededRandom(seed)) == draws(SeededRandom(seed))


def test_the_system_source_draws_from_its_range() -> None:
    source = SystemRandomSource()
    assert 0.0 <= source.random() < 1.0
    assert source.fork("anything") is source
    assert source.choice([4]) == 4
    assert 1 <= source.randint(1, 1) <= 1
    items = [1, 2, 3]
    source.shuffle(items)
    assert sorted(items) == [1, 2, 3]
