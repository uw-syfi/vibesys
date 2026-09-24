"""Built-in policy decisions with fake turns and durable effects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vibesys.loops.agent.hypothesis_controller import HypothesisEngine
from vibesys.loops.agent.model import AgentRunState
from vibesys.loops.agent.policy_attempts import (
    AttemptDecision,
    AttemptRequest,
    AttemptServices,
    AttemptState,
    execute_attempts,
)
from vibesys.loops.agent.policy_multi import MultiAgentAttemptPolicy, MultiAgentRoundPreparation
from vibesys.loops.agent.policy_profile import ProfileGuidedPolicy, ProfilePreparation
from vibesys.loops.agent.policy_rounds import RoundPreparationRequest, RoundPreparationServices
from vibesys.loops.agent.policy_single import SingleAgentAttemptPolicy
from vibesys.loops.agent.policy_support import _CarryOver, _ImplementerAttempt
from vibesys.loops.gates import FrameworkBenchmarkOutcome
from vibesys.schemas import (
    ImplementerResponse,
    JudgeResponse,
    OrchestratorPlan,
    PreRoundDecision,
    ProfilerSummary,
    SingleAgentRoundResponse,
    Verdict,
)

if TYPE_CHECKING:
    from vibesys.evaluators.input_manifest import ProfileGuidedInput
    from vibesys.loops.agent.policy_attempts import AttemptRunner


@dataclass
class _FakeTurns:
    calls: list[str]
    needs_profile: bool = True

    def pre_round_decision(
        self, _request: RoundPreparationRequest, *, has_history: bool
    ) -> PreRoundDecision:
        assert not has_history
        self.calls.append("prepass")
        return PreRoundDecision(
            need_profile=self.needs_profile, profile_focus="decode", reasoning="measure first"
        )

    def profile(self, _request: RoundPreparationRequest, focus: str) -> ProfilerSummary:
        self.calls.append(f"profile:{focus}")
        return ProfilerSummary(analysis="analysis", bottlenecks="decode", suggestions="cache")

    def implement(self, _request: AttemptRequest, state: AttemptState) -> _ImplementerAttempt:
        self.calls.append(f"implement:{state.retry}")
        return _ImplementerAttempt(
            ImplementerResponse(summary="changed", expected_behavior="faster"), synthesized=False
        )

    def judge(
        self, _request: AttemptRequest, state: AttemptState, _conflict: str | None
    ) -> JudgeResponse:
        self.calls.append(f"judge:{state.retry}")
        return JudgeResponse(analysis="reviewed", feedback="", verdict=Verdict.PASS)

    def combined(
        self, _request: AttemptRequest, _state: AttemptState, /
    ) -> SingleAgentRoundResponse:
        raise AssertionError


@dataclass
class _FakeEffects:
    calls: list[str]
    first_unpaid: int = 1
    validation_feedback: list[str | None] = field(default_factory=list)
    gate_feedback: list[str | None] = field(default_factory=list)

    def checkpoint(self, _request: AttemptRequest, state: AttemptState) -> None:
        self.calls.append(f"checkpoint:{state.retry}")

    def record_official_decision(
        self,
        _request: AttemptRequest,
        state: AttemptState,
        *,
        run: bool,
        reason: str,
        provisional_candidates: int,
    ) -> None:
        assert provisional_candidates == 0
        self.calls.append(f"official-decision:{state.retry}:{run}:{reason}")

    def record_judge_skipped(self, _request: AttemptRequest, outcome: str) -> None:
        self.calls.append(f"judge-skipped:{outcome}")

    def validate(
        self, _request: AttemptRequest, state: AttemptState, _recipe: str | None
    ) -> str | None:
        self.calls.append(f"validate:{state.retry}")
        return self.validation_feedback.pop(0) if self.validation_feedback else None

    def current_commit(self) -> str:
        self.calls.append("commit")
        return "a" * 40

    def official_gates(
        self,
        _request: AttemptRequest,
        state: AttemptState,
        *,
        reuse_accuracy_pass: bool,
        candidate_commit: str | None,
    ) -> tuple[str | None, FrameworkBenchmarkOutcome, bool]:
        assert candidate_commit == "a" * 40
        assert not reuse_accuracy_pass
        self.calls.append(f"official-gate:{state.retry}")
        feedback = self.gate_feedback.pop(0) if self.gate_feedback else None
        return feedback, FrameworkBenchmarkOutcome(), feedback is None

    def next_attempt(self, round_number: int) -> int:
        self.calls.append(f"next:{round_number}")
        return self.first_unpaid

    def log(self, _message: str) -> None:
        self.calls.append("log")


def _attempt() -> tuple[AttemptRequest, AttemptState]:
    plan = OrchestratorPlan(
        hypothesis_id="h1",
        hypothesis="cache decode",
        task="implement cache",
        pass_criteria="tests pass",  # noqa: S106
        reasoning="measure decode",
    )
    engine = HypothesisEngine.create(AgentRunState(), config=None).start(plan, started_round=1)
    hypothesis = engine.state.active_hypothesis
    assert hypothesis is not None
    return (
        AttemptRequest(
            round_number=1,
            plan=plan,
            planned_official_reason="final_round",
            records=[],
            active_hypothesis=hypothesis,
            engine=engine,
            last_profile_focus="decode",
        ),
        AttemptState(agent_run_state=engine.state, feedback=None),
    )


def _services(turns: _FakeTurns, effects: _FakeEffects, *, retries: int = 3) -> AttemptServices:
    return AttemptServices(
        turns=turns,
        effects=effects,
        max_rounds=1,
        max_retries_per_round=retries,
        judge_every=1,
        official_eval_every=1,
    )


def test_multi_prepass_profiles_only_when_requested_and_enabled() -> None:
    calls: list[str] = []
    turns = _FakeTurns(calls)
    request = RoundPreparationRequest(
        round_number=1, records=[], carry=_CarryOver(), previous_single_response=None
    )

    enabled = MultiAgentRoundPreparation(RoundPreparationServices(turns, profiler_enabled=True))
    assert enabled.profiler_summary(request) is not None
    assert calls == ["prepass", "profile:decode"]

    calls.clear()
    turns.needs_profile = False
    assert enabled.profiler_summary(request) is None
    assert calls == ["prepass"]

    calls.clear()
    turns.needs_profile = True
    disabled = MultiAgentRoundPreparation(RoundPreparationServices(turns, profiler_enabled=False))
    assert disabled.profiler_summary(request) is None
    assert calls == ["prepass"]


def test_profile_guidance_uses_effect_port_before_designer() -> None:
    from vibesys.evaluators.input_manifest import ProfileGuidedInput  # noqa: PLC0415

    class FakeProfileEffect:
        def prepare_profile(
            self,
            engine: HypothesisEngine,
            state: AgentRunState,
            settings: ProfileGuidedInput,
            round_number: int,
        ) -> tuple[HypothesisEngine, AgentRunState]:
            calls.append(f"profile-prepared:{round_number}:{settings.command[0]}")
            return engine, state

    calls: list[str] = []
    settings = ProfileGuidedInput(command=("fake-profiler",))
    engine = HypothesisEngine.create(AgentRunState(), config=settings)
    policy = ProfileGuidedPolicy(settings)
    prepared, state = policy.prepare(
        ProfilePreparation(FakeProfileEffect(), engine, engine.state, round_number=1)
    )

    assert prepared is engine
    assert state == engine.state
    assert calls == ["profile-prepared:1:fake-profiler"]


def test_multi_validation_failure_checkpoints_before_retry_and_official_gate() -> None:
    calls: list[str] = []
    turns = _FakeTurns(calls)
    effects = _FakeEffects(calls, validation_feedback=["bad recipe", None])
    services = _services(turns, effects, retries=2)
    request, state = _attempt()

    execute_attempts(MultiAgentAttemptPolicy(services), services, request, state)

    assert state.passed
    assert calls.index("checkpoint:1") < calls.index("implement:2")
    assert [call for call in calls if call.startswith(("implement:", "judge:", "validate:"))] == [
        "implement:1",
        "judge:1",
        "validate:1",
        "implement:2",
        "judge:2",
        "validate:2",
    ]
    assert calls.index("official-decision:2:True:final_round") < calls.index("official-gate:2")


def test_single_policy_uses_only_combined_turn_and_its_own_verdict() -> None:
    @dataclass
    class SingleTurns(_FakeTurns):
        def combined(
            self, _request: AttemptRequest, state: AttemptState, /
        ) -> SingleAgentRoundResponse:
            self.calls.append(f"combined:{state.retry}")
            return SingleAgentRoundResponse(
                summary="changed",
                expected_behavior="faster",
                self_review="checked",
                feedback="",
                verdict=Verdict.PASS,
                bottlenecks="decode",
                suggestions="cache",
                profile_analysis="measured",
            )

    calls: list[str] = []
    services = _services(SingleTurns(calls), _FakeEffects(calls))
    request, state = _attempt()
    state.retry = 1

    decision = SingleAgentAttemptPolicy(services).run_attempt(request, state)

    assert decision is AttemptDecision.OFFICIAL
    assert calls == ["combined:1"]


def test_resume_marker_skips_paid_turn_and_retries_failed_official_gate() -> None:
    @dataclass
    class FakeFlow:
        calls: list[str]

        def run_attempt(self, _request: AttemptRequest, state: AttemptState) -> AttemptDecision:
            self.calls.append(f"policy:{state.retry}")
            if state.retry == 2:
                state.official_reason = "cadence"
                return AttemptDecision.OFFICIAL
            state.passed = True
            return AttemptDecision.FINISH

    calls: list[str] = []
    turns = _FakeTurns(calls)
    effects = _FakeEffects(calls, first_unpaid=2, gate_feedback=["accuracy failed"])
    services = _services(turns, effects)
    request, state = _attempt()
    flow: AttemptRunner = FakeFlow(calls)

    execute_attempts(flow, services, request, state)

    assert state.retry == 3
    assert state.passed
    assert state.feedback == "accuracy failed"
    assert calls == [
        "next:1",
        "log",
        "log",
        "policy:2",
        "official-decision:2:True:cadence",
        "commit",
        "official-gate:2",
        "checkpoint:2",
        "log",
        "policy:3",
    ]
