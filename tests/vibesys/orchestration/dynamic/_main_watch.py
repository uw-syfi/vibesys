"""What a merge story checks of main each time the search publishes its record.

Main moves only to the commit of a merge that merged, new work starts from main, and each merge
kept the rule, restated from the host's own numbers rather than read from the search.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tests.vibesys.orchestration.dynamic._host import CANDIDATE_THROUGHPUT, INPUT_THROUGHPUT

from vibesys.orchestration.dynamic.search.api import MergeOutcome, WorkKind

if TYPE_CHECKING:
    from vibesys.orchestration.dynamic.search.api import RunSnapshot
    from vs_core.api import RevisionRef


@dataclass
class MainWatch:
    """What the stories saw of main as the search published its record."""

    throughputs: dict[str, float]
    """The throughput the host measures for a candidate, by revision id (else the default)."""
    input_throughput: float | None = INPUT_THROUGHPUT
    """The input's measured throughput; None when the input is unmeasurable."""
    mains: list[RevisionRef] = field(default_factory=list)
    """Main at each publication, in order; the first is the run's input."""
    seen: set[int] = field(default_factory=set)
    """The sequences of the workstreams already checked."""

    def check(self, record: RunSnapshot | None) -> None:
        """Main moves only to the commit of a merge that merged; new work starts from main."""
        if record is None or record.main is None:
            return
        before = self.mains[-1] if self.mains else record.main
        self.mains.append(record.main)
        merged_ = [item.revision for item in record.merges if item.outcome is MergeOutcome.MERGED]
        # Main is the commit of the last merge that merged, else still the input.
        assert record.main == (merged_[-1] if merged_ else self.mains[0]), record.merges
        moves = sum(
            1 for prior, now in zip(self.mains, self.mains[1:], strict=False) if now != prior
        )
        assert moves <= len(merged_)
        others = [item for item in record.merges if item.outcome is not MergeOutcome.MERGED]
        assert all(item.revision not in self.mains for item in others if item.revision)
        self._merges_kept_the_rule(record)
        for running in record.running:
            if running.sequence in self.seen:
                continue
            self.seen.add(running.sequence)
            plan = running.plan
            fresh = plan.kind is WorkKind.MERGE or (
                plan.kind is WorkKind.IMPLEMENT
                and not plan.continue_hypothesis
                and plan.parent_hypothesis_id is None
            )
            if fresh:
                # Scheduled in this turn of the root or the one before: main then or now.
                assert running.parent in {before, record.main}, (running, before, record.main)

    def _merges_kept_the_rule(self, record: RunSnapshot) -> None:
        """Each merge named only mergeable candidates, and only a sound one moved main.

        The rule is restated from the host's own numbers, not read from the search: a candidate
        is no merged main, was not merged before, and measures above main (anything does while
        main and the input have no throughput); a merge that merged passed accuracy and measures
        no lower than main before it.
        """
        merge_ids = {item.merge_id for item in record.merges} | {
            item.plan.work_id for item in record.running if item.plan.kind is WorkKind.MERGE
        }
        main = self.input_throughput
        merged: set[tuple[str, str]] = set()
        for item in sorted(record.merges, key=lambda merge_: merge_.sequence):
            for source in item.candidates:
                root = source.revision.revision_id.root
                assert source.hypothesis_id not in merge_ids, item
                assert (source.hypothesis_id, root) not in merged, item
                found = self.throughputs.get(root, CANDIDATE_THROUGHPUT)
                assert main is None or found > main, (item, main)
            if item.outcome is not MergeOutcome.MERGED:
                continue
            assert item.accuracy_passed is True, item
            assert item.benchmark_passed is True, item
            assert item.metrics, item
            assert main is None or item.metrics[0].value >= main, (item, main)
            main = item.metrics[0].value
            merged |= {(s.hypothesis_id, s.revision.revision_id.root) for s in item.candidates}
