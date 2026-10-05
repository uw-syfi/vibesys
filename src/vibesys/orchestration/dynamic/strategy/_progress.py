"""When a workstream whose turn ended without a measurable candidate may be asked again.

A turn that ends without a candidate reaching measurement spends agent time and tokens
and produces no evidence. Asking again is worth it only when the next turn has something
the last one lacked, so the policy is data: the history of such turns (`Blocker`) and one
bound from `DynamicConfig.max_unmeasured_turns`. When it refuses, the workstream settles as
failed with the returned reason, and its slot goes back to the planner.
"""

from vibesys.orchestration.dynamic.strategy._config import DynamicConfig
from vibesys.orchestration.dynamic.strategy._state import Blocker, BlockerKind


def _normalized(text: str) -> str:
    return " ".join(text.lower().split())


def _new_information(earlier: tuple[Blocker, ...], latest: Blocker) -> bool:
    """Whether the latest turn names a narrower scope or cites evidence not seen before.

    A reviewer's feedback is information the implementer did not have, so a rejection
    always counts. An implementer's failure counts only when it states a next step that no
    earlier turn stated, or cites a location no earlier turn cited.
    """
    if latest.kind is BlockerKind.REJECTED:
        return True
    steps = {_normalized(item.next_step) for item in earlier if item.next_step.strip()}
    narrowed = bool(latest.next_step.strip()) and _normalized(latest.next_step) not in steps
    seen = {location for item in earlier for location in item.cited}
    return narrowed or any(location not in seen for location in latest.cited)


def refusal(blockers: tuple[Blocker, ...], config: DynamicConfig) -> str | None:
    """Why the workstream must not be asked again, or None when another turn may run.

    ``blockers`` includes the turn that just ended as its last item.
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
    if not _new_information(blockers[:-1], latest):
        return (
            "the implementer ended without a candidate and named no narrower next step or new "
            f"evidence, so another turn would repeat it; its blocker: {latest.summary}"
        )
    return None
