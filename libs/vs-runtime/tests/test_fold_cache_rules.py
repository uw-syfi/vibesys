"""A start uses a cache only if the rules that fold now are the rules that made it (#3475).

``state.pb`` is a cache of the fold of a log. A process whose reducers changed since the cache was
written (a fix between a pause and the Resume) would fold the records after it with new rules over a
state the old rules made, and get a state its own fold of the whole log would not. The cache names
its rules, and a start under other rules folds the whole log.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st

from vs_core.api.recovery import Folded, RunRest, at_rest, fold_run, starts_run
from vs_runlog.api import CachedFold, CacheMiss, MemoryFoldCache, PrefixDigest
from vs_runtime.api.step_runner import CacheUsed, start_fold
from vs_sim.api.testing import examples

from .test_run_log_step_log_cache import display, operator, process

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_proto.api import Record
    from vs_runtime.api.testing import FakeRunSeed


def a_log(steps: int, extra: int) -> tuple[Sequence[Record], FakeRunSeed]:
    """The records of a run that began, was paused once, and showed ``extra`` more records."""
    run = process(steps)
    log, _ = run.start(cached=False)
    log.input(operator("pause", 1.0, 1))
    for _ in range(extra):
        display(run)
    return run.records.read(), run.seed


def seeded_from(records: Sequence[Record]) -> int:
    """How many records a prefix needs to hold the start of the run: the least a fold can begin from."""
    return next(number for number, record in enumerate(records, 1) if starts_run(record))


def cache_of(
    records: Sequence[Record],
    seed: FakeRunSeed,
    covering: int,
    rules: str,
    *,
    altered: bool = False,
) -> MemoryFoldCache:
    """A cache of the first ``covering`` records, folded by ``rules``.

    ``altered`` is a state the rules of another version made: not the one these rules make.
    """
    digest = PrefixDigest()
    digest.extend(records[:covering])
    folded = fold_run(records[:covering], registry=seed.registry)
    if altered:
        run = folded.state.run.model_copy(update={"now_at": folded.state.run.now_at + 1000.0})
        folded = Folded(folded.state.model_copy(update={"run": run}), folded.owed)
    cache = MemoryFoldCache()
    cache.store(CachedFold(digest.count, digest.value(), folded.encode(rules)))
    return cache


RULES = st.text(alphabet="abc", max_size=2)


@settings(max_examples=examples(pr=30, nightly=300), deadline=None)
@given(
    written=RULES,
    reading=RULES,
    steps=st.integers(1, 3),
    extra=st.integers(0, 4),
    data=st.data(),
)
def test_a_start_uses_a_cache_only_under_the_rules_that_made_it(
    written: str, reading: str, steps: int, extra: int, data: st.DataObject
) -> None:
    records, seed = a_log(steps, extra)
    covering = data.draw(st.integers(seeded_from(records), len(records)))
    cache = cache_of(records, seed, covering, written)

    folded, keeper = start_fold(cache, records, seed.registry, rules=reading)

    assert folded == fold_run(records, registry=seed.registry), "a cache changed the fold"
    assert keeper is not None
    if written == reading and reading:
        assert keeper.start == CacheUsed(covering)
    else:
        assert keeper.start is CacheMiss.RULES_CHANGED


@settings(max_examples=examples(pr=10, nightly=100), deadline=None)
@given(steps=st.integers(1, 3), extra=st.integers(1, 4), data=st.data())
def test_a_state_an_older_version_made_is_never_the_state_a_start_folds_from(
    steps: int, extra: int, data: st.DataObject
) -> None:
    """The cache is the fold of the log's own first records by the older rules, which differ.

    Keyed by the log's bytes alone it matched, and the start went on from a state its own fold of the
    whole log does not make.
    """
    records, seed = a_log(steps, extra)
    covering = data.draw(st.integers(seeded_from(records), len(records) - 1))
    cache = cache_of(records, seed, covering, "old rules", altered=True)

    folded, keeper = start_fold(cache, records, seed.registry, rules="new rules")

    assert folded == fold_run(records, registry=seed.registry)
    assert keeper is not None
    assert keeper.start is CacheMiss.RULES_CHANGED


def test_a_cache_the_keeper_writes_is_used_by_the_next_start_of_the_same_rules() -> None:
    """A run that parks leaves a cache under this code's rules: the next start of this code uses it,
    and a start of other code folds the whole log."""
    run = process(2)
    log, _ = run.start()
    log.input(operator("pause", 1.0, 1))
    assert at_rest(log.state) is RunRest.PARKED
    records, registry = run.records.read(), run.seed.registry
    whole = fold_run(records, registry=registry)

    same, used = start_fold(run.cache, records, registry)
    assert used is not None
    assert isinstance(used.start, CacheUsed)
    assert same == whole

    other, changed = start_fold(run.cache, records, registry, rules="other code")
    assert changed is not None
    assert changed.start is CacheMiss.RULES_CHANGED
    assert other == whole


def test_the_rules_of_a_build_with_no_source_match_no_cache() -> None:
    records, seed = a_log(1, 1)
    cache = cache_of(records, seed, seeded_from(records), "")
    _, keeper = start_fold(cache, records, seed.registry, rules="")
    assert keeper is not None
    assert keeper.start is CacheMiss.RULES_CHANGED
