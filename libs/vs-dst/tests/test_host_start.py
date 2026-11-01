"""A host starts its run as the product does: over the fold cache the dead host left.

The step host opens its log with ``vs_runtime``'s ``start_run`` and resumes a paused run with
``resume_paused``, the two steps ``drive_core_run`` takes, over a run.log and a ``state.pb`` kept
in memory. A kill lands after a cache write and before the next record, so what these cases hold
is what no unit case of the cache does: a start over a cache that is behind the log by any number
of records folds to the state the whole log folds to.
"""

from __future__ import annotations

from vs_dst.api import CrashKind, CrashPoint, StepLoopHosting, simulate
from vs_runlog.api import CacheMiss
from vs_runtime.api.step_runner import CacheUsed

from ._tiny import TinyWorld

STEP_LOOP = StepLoopHosting()


def test_a_host_taking_over_after_a_cache_write_folds_only_what_came_after_it() -> None:
    """Killed late in the run, the next host starts from ``state.pb`` and ends the run as it ended.

    The first host finds no cache. Each host that follows one that wrote it starts from it, and
    the log it folds from there is never shorter than the cache covers: a cache ahead of the log
    would be a miss.
    """
    normal = simulate(TinyWorld(3), STEP_LOOP)
    assert normal.cache_starts == (CacheMiss.ABSENT,)

    late = CrashPoint(CrashKind.AFTER_COMMIT, normal.commits - 1)
    recovered = normal.rerun_with_crash(late)

    *earlier, takeover = recovered.cache_starts
    assert earlier == [CacheMiss.ABSENT]
    assert isinstance(takeover, CacheUsed)
    assert 0 < takeover.through_seq <= late.commit + 1
    assert recovered.finished
    assert recovered.result == normal.result


def test_a_takeover_over_a_cache_behind_the_log_ends_the_run_as_before() -> None:
    """Every crash point the policy offers: the takeover's start is the cache or the whole log, and the end is the same."""
    normal = simulate(TinyWorld(3), STEP_LOOP)
    used = 0

    for crash in normal.crash_points():
        recovered = normal.rerun_with_crash(crash)
        takeover = recovered.cache_starts[-1]
        if isinstance(takeover, CacheUsed):
            used += 1
            assert takeover.through_seq <= crash.commit + 1, crash
        else:
            assert takeover is CacheMiss.ABSENT, crash
        assert recovered.finished, crash
        assert recovered.result == normal.result, crash
        assert recovered.no_effect_ran_twice(), crash

    assert used > 0
