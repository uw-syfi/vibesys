"""state.pb for a run's step log: start from the cache, and keep it.

A start reads the cache, checks it is the fold of the log's own first records, and folds only the
records after it. The keeper then writes the cache again when the run comes to rest (pause, end)
and every ``every`` records its process appended, so a later start folds the records since.

The keeper folds the log itself, record by record from where its cache stopped, and not by the
step log's state: the log holds records that are not core's (the display projection), which a fold
reads for their epoch and clock, and the step log's own fold has not applied those. A cache is
written from the fold a later start would compute, so it is that fold whatever else the log holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_core.api.recovery import Folded
from vs_runlog.api import CachedFold, CacheMiss, PrefixDigest, judge
from vs_runtime._saved_run_record import fold_log

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_core.api import OperationRegistry
    from vs_proto.api import Record
    from vs_runlog.api import ConfirmedLog, FoldCache

CACHE_EVERY = 1024
"""Core records a process appends between two writes of the cache while the run goes on.

A fold costs 0.3 to 0.6 milliseconds a core record (run-recovery.md), so a start folds about half a
second of records at most."""


@dataclass(frozen=True)
class CacheUsed:
    """The start folded only the records after the first ``through_seq``."""

    through_seq: int


type FoldStart = CacheUsed | CacheMiss
"""How a start got its fold: from a cache, or from the whole log for a typed reason."""


class FoldKeeper:
    """The cache of a run's fold: what a start used of it, and when it is written again."""

    def __init__(
        self,
        cache: FoldCache,
        digest: PrefixDigest,
        kept: Folded | None,
        start: FoldStart,
        *,
        every: int,
    ) -> None:
        """Keep ``kept``, the fold of the first ``digest.count`` records, in ``cache``."""
        self._cache = cache
        self._digest = digest
        self._kept = kept
        self._every = every
        self._written_at = 0
        self.start = start

    def keep(
        self,
        log: ConfirmedLog,
        registry: OperationRegistry,
        *,
        appended: int,
        resting: bool,
    ) -> None:
        """Write the cache if the run is at rest, or if ``every`` records came since the last write.

        ``appended`` is how many core records this process has appended; ``resting`` is whether
        the run is parked or ended. A write that fails leaves the cache before it, which is only
        older, and the next record tries again.
        """
        if appended == self._written_at or not (
            resting or appended - self._written_at >= self._every
        ):
            return
        tail = log.records()[self._digest.count :]
        self._digest.extend(tail)
        self._kept = fold_log(tail, registry, self._kept)
        self._written_at = appended
        if self._kept is None:
            return
        cached = CachedFold(self._digest.count, self._digest.value(), self._kept.encode())
        try:
            self._cache.store(cached)
        except OSError:
            return


def start_fold(
    cache: FoldCache | None,
    prefix: Sequence[Record],
    registry: OperationRegistry,
    *,
    every: int = CACHE_EVERY,
) -> tuple[Folded | None, FoldKeeper | None]:
    """The fold of ``prefix``, None while it holds no seed, and the keeper that goes on from it.

    With a cache that is the fold of ``prefix``'s own first records, only the records after it are
    folded. A cache that is not (see ``CacheMiss``), or whose fold cannot be read, is ignored and
    the whole of ``prefix`` is folded: the result is the same.
    """
    if cache is None:
        return fold_log(prefix, registry), None
    digest = PrefixDigest()
    verdict = judge(cache.load(), prefix, digest)
    if isinstance(verdict, CachedFold):
        try:
            cached = Folded.decode(verdict.state, registry)
        except ValueError:
            verdict = CacheMiss.STATE_REFUSED
        else:
            folded = fold_log(prefix[verdict.through_seq :], registry, cached)
            start = CacheUsed(verdict.through_seq)
            return folded, FoldKeeper(cache, digest, folded, start, every=every)
    folded = fold_log(prefix, registry)
    return folded, FoldKeeper(cache, digest, folded, verdict, every=every)
