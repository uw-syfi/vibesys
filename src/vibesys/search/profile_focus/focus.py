"""``ProfileFocus``: pure profile-guided component-selection policy.

Ported from ``ProfileGuidedHypothesisController``, ``ProfileGuidanceOutcome``,
``_merge_attribution``, ``_select_component``, and ``_format_ledger`` in
``loops/profile_multi/controller.py``. Independent of
:class:`~vibesys.search.hypothesis.search.HypothesisSearch`; orchestration
composes the two (profile focus decides *which component* the round targets,
hypothesis search decides the round's lifecycle).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.search.profile_focus.results import FocusView
from vibesys.search.profile_focus.state import (
    ProfileAttributionSample,
    ProfileBottleneck,
    ProfileFocusState,
    ProfileGuidanceStatus,
    ProfileGuidedComponent,
    ProfileImprovementSample,
)

if TYPE_CHECKING:
    from vibesys.search.profile_focus.config import ProfileFocusConfig

__all__ = ["ProfileFocus"]


@dataclass(frozen=True, slots=True)
class ProfileFocus:
    """Pure profile-guided focus policy bound to one strategy's configuration."""

    config: ProfileFocusConfig

    def initial(self) -> ProfileFocusState:
        """Return the empty starting state for a new run."""
        return ProfileFocusState()

    def observe(
        self,
        state: ProfileFocusState,
        *,
        round_number: int,
        bottlenecks: tuple[ProfileBottleneck, ...],
        override: str | None = None,
    ) -> ProfileFocusState:
        """Merge one round's attribution and select exactly one open component.

        ``override`` names a component to force active (when still open); it
        loses to nothing else, but an exhausted override is ignored the same
        as an exhausted current selection, falling through to the first open
        component.
        """
        merged = _merge_attribution(state, bottlenecks, round_number=round_number)
        return _select_component(merged, override=override)

    def focus(self, state: ProfileFocusState) -> FocusView:
        """Render prompt guidance solely from the persisted cursor.

        ``ranked_bottlenecks`` is rebuilt from each component's latest
        attribution sample rather than held as a separate ephemeral field
        (as the ported ``ProfileGuidedHypothesisController`` did), so
        ``focus`` is a pure function of the persisted state alone.
        """
        return FocusView(
            active_component=state.active_component or "",
            ledger_text=_format_ledger(state),
            ranked_bottlenecks=tuple(_ranked_bottlenecks(state)),
        )

    def record(
        self,
        state: ProfileFocusState,
        *,
        round_number: int,
        passed: bool,
        relative_improvement: float | None,
    ) -> ProfileFocusState:
        """Apply one completed round's outcome to the active component.

        A component that has stalled for ``plateau_min_rounds`` in a row is
        marked exhausted and never reselected by :meth:`observe`.
        """
        if state.active_component is None or not passed or relative_improvement is None:
            return state
        updated = state.model_copy(deep=True)
        component = next(
            item for item in updated.components if item.name == updated.active_component
        )
        component.rounds_spent += 1
        component.improvement_history.append(
            ProfileImprovementSample(round=round_number, relative_improvement=relative_improvement)
        )
        if relative_improvement >= self.config.min_relative_improvement:
            component.stalled_rounds = 0
        else:
            component.stalled_rounds += 1
        if component.stalled_rounds >= self.config.plateau_min_rounds:
            component.status = ProfileGuidanceStatus.EXHAUSTED
            updated.active_component = None
        return ProfileFocusState.model_validate(updated.model_dump())


def _merge_attribution(
    state: ProfileFocusState,
    attribution: tuple[ProfileBottleneck, ...],
    *,
    round_number: int,
) -> ProfileFocusState:
    existing = {component.name: component.model_copy(deep=True) for component in state.components}
    ordered: list[ProfileGuidedComponent] = []
    for item in attribution:
        component = existing.pop(item.name, ProfileGuidedComponent(name=item.name))
        component.latest_cost = item.cost
        component.latest_share = item.share
        component.attribution_history = [
            sample for sample in component.attribution_history if sample.round != round_number
        ]
        component.attribution_history.append(
            ProfileAttributionSample(
                round=round_number, cost=item.cost, share=item.share, evidence=item.evidence
            )
        )
        ordered.append(component)
    ordered.extend(existing.values())
    return ProfileFocusState(active_component=state.active_component, components=ordered)


def _select_component(state: ProfileFocusState, *, override: str | None) -> ProfileFocusState:
    updated = state.model_copy(deep=True)
    chosen = next(
        (
            item
            for item in updated.components
            if item.name == override and item.status is not ProfileGuidanceStatus.EXHAUSTED
        ),
        None,
    )
    if chosen is None:
        chosen = next(
            (
                item
                for item in updated.components
                if item.name == state.active_component
                and item.status is not ProfileGuidanceStatus.EXHAUSTED
            ),
            None,
        )
    if chosen is None:
        chosen = next(
            (
                item
                for item in updated.components
                if item.status is not ProfileGuidanceStatus.EXHAUSTED
            ),
            None,
        )
    for component in updated.components:
        if component.status is ProfileGuidanceStatus.ACTIVE:
            component.status = ProfileGuidanceStatus.OPEN
    if chosen is None:
        updated.active_component = None
    else:
        chosen.status = ProfileGuidanceStatus.ACTIVE
        updated.active_component = chosen.name
    return ProfileFocusState.model_validate(updated.model_dump())


def _format_ledger(state: ProfileFocusState) -> str:
    if not state.components:
        return ""
    lines = ["component | status | rounds_spent | latest_share | stalled_rounds"]
    for component in state.components:
        share = (
            f"{component.latest_share * 100:.2f}%" if component.latest_share is not None else "-"
        )
        lines.append(
            f"{component.name} | {component.status.value} | {component.rounds_spent} | "
            f"{share} | {component.stalled_rounds}"
        )
    return "\n".join(lines)


def _ranked_bottlenecks(state: ProfileFocusState) -> list[ProfileBottleneck]:
    """Rebuild the last-observed attribution ranking from persisted components.

    Mirrors ``parse_attribution``'s ordering (cost descending, then name) so
    a round that never called :meth:`ProfileFocus.observe` again still shows
    the same ranking it last saw.
    """
    bottlenecks = [
        ProfileBottleneck(
            name=component.name,
            cost=component.latest_cost,
            share=component.latest_share,
            evidence=list(component.attribution_history[-1].evidence)
            if component.attribution_history
            else [],
        )
        for component in state.components
        if component.latest_cost is not None and component.latest_share is not None
    ]
    return sorted(bottlenecks, key=lambda item: (-item.cost, item.name))
