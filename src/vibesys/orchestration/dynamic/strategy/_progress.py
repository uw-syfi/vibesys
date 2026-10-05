"""When a workstream whose turn ended without a measurable candidate may be asked again.

A turn that ends without a candidate reaching measurement spends agent time and tokens
and produces no evidence. The first such turn always gets one more turn. Further turns are
worth it only when the last one left something the framework itself observed that no earlier
turn did: a retained revision other than the parent's and every earlier turn's, or a failure
output not seen before. What the agent says about its next step goes into the retry prompt
but never earns a turn, because rewording is free. The policy is data: the history of such
turns (`Blocker`) and one bound, `DynamicConfig.max_unmeasured_turns`. When it refuses, the
workstream settles as failed with the returned reason, and its slot goes back to the planner.
"""

from vibesys.orchestration.dynamic.strategy._config import DynamicConfig
from vibesys.orchestration.dynamic.strategy._state import Blocker
from vs_core.api import RevisionRef


def _observed_news(
    earlier: tuple[Blocker, ...], latest: Blocker, parent: RevisionRef | None
) -> bool:
    """Whether the latest turn retained a new revision or produced a new failure output."""
    new_revision = (
        latest.revision is not None
        and latest.revision != parent
        and all(latest.revision != item.revision for item in earlier)
    )
    new_output = bool(latest.digest) and all(latest.digest != item.digest for item in earlier)
    return new_revision or new_output


def refusal(
    blockers: tuple[Blocker, ...], config: DynamicConfig, parent: RevisionRef | None
) -> str | None:
    """Why the workstream must not be asked again, or None when another turn may run.

    ``blockers`` includes the turn that just ended as its last item; ``parent`` is the
    revision the workstream builds on.
    """
    if not blockers:
        return None
    latest = blockers[-1]
    spent = len(blockers)
    if spent >= config.max_unmeasured_turns:
        return (
            f"{spent} implementer turns ended without a candidate that reached measurement "
            f"(limit {config.max_unmeasured_turns}); last blocker: {latest.summary}"
        )
    if spent > 1 and not _observed_news(blockers[:-1], latest, parent):
        return (
            "the last turn ended without a candidate that reached measurement and left no "
            "retained revision or failure output that an earlier turn did not, so another "
            f"turn would repeat it; its blocker: {latest.summary}"
        )
    return None
