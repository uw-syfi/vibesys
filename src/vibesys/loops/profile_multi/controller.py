"""Profile-guided multi cursor and hypothesis transitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, Self, runtime_checkable

from vibesys.agent_run.hypotheses import append_round, start_hypothesis, update_active_hypothesis
from vibesys.agent_run.state import (
    AgentRunState,
    ProfileAttributionSample,
    ProfileBottleneck,
    ProfileGuidanceState,
    ProfileGuidanceStatus,
    ProfileGuidedComponent,
    ProfileImprovementSample,
)

if TYPE_CHECKING:
    from vibesys.agent_run.state import Hypothesis
    from vibesys.evaluators.input_manifest import ProfileGuidedInput
    from vibesys.schemas import OrchestratorPlan
    from vs_loop_state.api import RoundRecord


@dataclass(frozen=True)
class ProfileGuidanceView:
    """Ephemeral prompt inputs derived from authoritative policy state."""

    active_component: str = ""
    ledger_text: str = ""
    ranked_bottlenecks: tuple[ProfileBottleneck, ...] = ()

    def plan_prompt_context(self) -> dict[str, object]:
        """Return variables consumed by the orchestrator plan template."""
        return {
            "active_component": self.active_component,
            "ledger_text": self.ledger_text,
            "ranked_bottlenecks": [
                {
                    "component": item.name,
                    "cost_share": item.share * 100,
                    "evidence": item.evidence,
                }
                for item in self.ranked_bottlenecks
            ],
        }

    def implementer_prompt_context(self) -> dict[str, object]:
        """Return variables consumed by the implementer template."""
        return {"active_component": self.active_component}


@runtime_checkable
class HypothesisController(Protocol):
    """Pure interface for selecting and advancing hypothesis policy state."""

    @property
    def state(self) -> AgentRunState:
        """Return the controller's detached authoritative aggregate."""
        ...

    @property
    def guidance(self) -> ProfileGuidanceView:
        """Return prompt guidance derived from the current aggregate."""
        ...

    def prepare_round(
        self,
        *,
        round_number: int,
        attribution: tuple[ProfileBottleneck, ...],
        override: str | None = None,
    ) -> Self:
        """Return a controller with a refreshed profile ranking and cursor."""
        ...

    def advance_round(
        self,
        *,
        round_number: int,
        passed: bool,
        relative_improvement: float | None,
    ) -> Self:
        """Return a controller with one completed round applied to its cursor."""
        ...

    def replace_state(self, state: AgentRunState) -> Self:
        """Return an equivalent policy controller over ``state``."""
        ...


@dataclass(frozen=True)
class ProfileGuidanceOutcome:
    """Trusted inputs for advancing policy with a completed round."""

    round_number: int
    passed: bool
    relative_improvement: float | None

    @classmethod
    def from_round(
        cls,
        round_number: int,
        passed: bool,  # noqa: FBT001
        official: bool,  # noqa: FBT001
        delta_pct: float | None,
    ) -> ProfileGuidanceOutcome:
        """Normalize one completed round for profile policy advancement."""
        improvement = delta_pct / 100 if delta_pct is not None else None
        return cls(round_number, passed and official, improvement)


@dataclass(frozen=True)
class ProfileGuidedHypothesisController:
    """Immutable profile-guidance policy over the unified agent-run state."""

    _state: AgentRunState
    enabled: bool = False
    plateau_min_rounds: int = 2
    min_relative_improvement: float = 0.02
    _ranking: tuple[ProfileBottleneck, ...] = field(default=(), repr=False, compare=False)

    def __post_init__(self) -> None:
        """Validate policy configuration independently of input manifests."""
        if self.plateau_min_rounds < 1:
            raise ValueError("plateau_min_rounds must be positive")  # noqa: TRY003
        if self.min_relative_improvement < 0:
            raise ValueError("min_relative_improvement must be non-negative")  # noqa: TRY003

    @classmethod
    def create(
        cls,
        state: AgentRunState,
        *,
        enabled: bool = False,
        plateau_min_rounds: int = 2,
        min_relative_improvement: float = 0.02,
    ) -> ProfileGuidedHypothesisController:
        """Bind policy to a detached copy of an authoritative aggregate."""
        return cls(
            _state=state.clone(),
            enabled=enabled,
            plateau_min_rounds=plateau_min_rounds,
            min_relative_improvement=min_relative_improvement,
        )

    @property
    def state(self) -> AgentRunState:
        """Return a detached aggregate suitable for a typed state transition."""
        return self._state.clone()

    @property
    def guidance(self) -> ProfileGuidanceView:
        """Render prompt guidance solely from the persisted cursor."""
        policy = self._state.profile_guidance
        if policy is None:
            return ProfileGuidanceView()
        return ProfileGuidanceView(
            active_component=policy.active_component or "",
            ledger_text=_format_ledger(policy),
            ranked_bottlenecks=self._ranking,
        )

    def prepare_round(
        self,
        *,
        round_number: int,
        attribution: tuple[ProfileBottleneck, ...],
        override: str | None = None,
    ) -> ProfileGuidedHypothesisController:
        """Merge attribution and select exactly one non-exhausted component."""
        if not self.enabled:
            return self
        policy = _merge_attribution(
            self._state.profile_guidance or ProfileGuidanceState(),
            attribution,
            round_number=round_number,
        )
        policy = _select_component(policy, override=override)
        return self._replace_policy(policy, ranking=attribution)

    def advance_round(
        self,
        *,
        round_number: int,
        passed: bool,
        relative_improvement: float | None,
    ) -> ProfileGuidedHypothesisController:
        """Apply the completed-round outcome to the active profile component."""
        policy = self._state.profile_guidance
        if not self.enabled or policy is None or policy.active_component is None:
            return self
        updated = policy.model_copy(deep=True)
        component = next(
            item for item in updated.components if item.name == updated.active_component
        )
        if not passed or relative_improvement is None:
            return self
        component.rounds_spent += 1
        component.improvement_history.append(
            ProfileImprovementSample(
                round=round_number,
                relative_improvement=relative_improvement,
            )
        )
        if relative_improvement >= self.min_relative_improvement:
            component.stalled_rounds = 0
        else:
            component.stalled_rounds += 1
        if component.stalled_rounds >= self.plateau_min_rounds:
            component.status = ProfileGuidanceStatus.EXHAUSTED
            updated.active_component = None
        return self._replace_policy(ProfileGuidanceState.model_validate(updated.model_dump()))

    def _replace_policy(
        self,
        policy: ProfileGuidanceState,
        *,
        ranking: tuple[ProfileBottleneck, ...] = (),
    ) -> ProfileGuidedHypothesisController:
        state = self._state.model_copy(update={"profile_guidance": policy}, deep=True)
        return ProfileGuidedHypothesisController(
            _state=AgentRunState.model_validate(state.model_dump()),
            enabled=self.enabled,
            plateau_min_rounds=self.plateau_min_rounds,
            min_relative_improvement=self.min_relative_improvement,
            _ranking=ranking,
        )

    def replace_state(self, state: AgentRunState) -> ProfileGuidedHypothesisController:
        """Return the same policy configuration over a detached aggregate."""
        return ProfileGuidedHypothesisController(
            _state=state.clone(),
            enabled=self.enabled,
            plateau_min_rounds=self.plateau_min_rounds,
            min_relative_improvement=self.min_relative_improvement,
            _ranking=self._ranking,
        )


@dataclass(frozen=True)
class HypothesisEngine:
    """Compose ordinary hypothesis transitions with a selected policy controller."""

    controller: HypothesisController

    @property
    def state(self) -> AgentRunState:
        """Return the engine's detached authoritative aggregate."""
        return self.controller.state

    @classmethod
    def create(
        cls,
        state: AgentRunState,
        *,
        config: ProfileGuidedInput | None,
    ) -> HypothesisEngine:
        """Create the shared engine with either ordinary or profile-guided policy."""
        return cls(
            ProfileGuidedHypothesisController.create(
                state,
                enabled=config is not None,
                plateau_min_rounds=config.min_measured_rounds if config else 2,
                min_relative_improvement=config.min_relative_improvement if config else 0.02,
            )
        )

    def replace_state(self, state: AgentRunState) -> HypothesisEngine:
        """Keep the selected policy while adopting newer lifecycle state."""
        return HypothesisEngine(self.controller.replace_state(state))

    def start(
        self,
        plan: OrchestratorPlan,
        *,
        started_round: int,
        parent_round: int | None = None,
        parent_commit: str | None = None,
    ) -> HypothesisEngine:
        """Apply the existing pure hypothesis-start transition."""
        state = start_hypothesis(
            self.state,
            plan,
            started_round=started_round,
            parent_round=parent_round,
            parent_commit=parent_commit,
        )
        return HypothesisEngine(self.controller.replace_state(state))

    def replace_active(self, hypothesis: Hypothesis) -> HypothesisEngine:
        """Apply the existing pure active-checkpoint transition."""
        state = update_active_hypothesis(self.state, hypothesis)
        return HypothesisEngine(self.controller.replace_state(state))

    def complete_round(
        self,
        record: RoundRecord,
        *,
        next_active: Hypothesis | None,
        profile_outcome: ProfileGuidanceOutcome | None = None,
    ) -> HypothesisEngine:
        """Atomically compose round evidence, active state, and profile policy."""
        state = (
            update_active_hypothesis(self.state, next_active)
            if next_active is not None
            else self.state
        )
        controller = self.controller.replace_state(state)
        if profile_outcome is not None:
            controller = controller.advance_round(
                round_number=profile_outcome.round_number,
                passed=profile_outcome.passed,
                relative_improvement=profile_outcome.relative_improvement,
            )
        state = append_round(
            controller.state,
            record,
            keep_active=next_active is not None,
        )
        return HypothesisEngine(controller.replace_state(state))


def _merge_attribution(
    policy: ProfileGuidanceState,
    attribution: tuple[ProfileBottleneck, ...],
    *,
    round_number: int,
) -> ProfileGuidanceState:
    existing = {component.name: component.model_copy(deep=True) for component in policy.components}
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
                round=round_number,
                cost=item.cost,
                share=item.share,
                evidence=item.evidence,
            )
        )
        ordered.append(component)
    ordered.extend(existing.values())
    return ProfileGuidanceState(active_component=policy.active_component, components=ordered)


def _select_component(
    policy: ProfileGuidanceState,
    *,
    override: str | None,
) -> ProfileGuidanceState:
    updated = policy.model_copy(deep=True)
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
                if item.name == policy.active_component
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
    return ProfileGuidanceState.model_validate(updated.model_dump())


def _format_ledger(policy: ProfileGuidanceState) -> str:
    if not policy.components:
        return ""
    lines = ["component | status | rounds_spent | latest_share | stalled_rounds"]
    for component in policy.components:
        share = (
            f"{component.latest_share * 100:.2f}%" if component.latest_share is not None else "-"
        )
        lines.append(
            f"{component.name} | {component.status.value} | {component.rounds_spent} | "
            f"{share} | {component.stalled_rounds}"
        )
    return "\n".join(lines)
