"""Explicit designer, profiler, implementer, and judge conversations."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.constants import DomainName
from vibesys.orchestration.domains.base import DomainRole
from vibesys.orchestration.domains.registry import resolve_domain
from vibesys.orchestration.domains.rendering import render_domain_section
from vibesys.orchestration.hypothesis import (
    InvalidPlanError,
    OrchestratorPlan,
    SkillResourceSelection,
    normalize_hypothesis_title,
)
from vibesys.orchestration.multi.agents import DESIGNER, IMPLEMENTER, JUDGE, PROFILER
from vibesys.orchestration.multi.contracts import (
    ImplementerContext,
    ImplementerContinuationContext,
    ImplementerResponse,
    JudgeContext,
    JudgeResponse,
    PlanContext,
    PreRoundContext,
    PreRoundDecision,
    ProfilerContext,
)
from vibesys.orchestration.multi.prompts import (
    render_continuation_prompt,
    render_implementer_prompt,
    render_judge_prompt,
    render_plan_prompt,
    render_pre_round_prompt,
    render_profiler_prompt,
)
from vibesys.orchestration.profilers import (
    ProfilerKind,
    ProfilerSummary,
    UnsupportedProfilerError,
    profiler_definition,
)
from vibesys.orchestration.review import Verdict
from vs_agent.api import AgentUsageLimitError
from vs_loop_state.api import HypothesisOutcome
from vs_runtime.api import (
    AgentTurnTimeoutError,
    ResolvedSkillResources,
    Run,
    SkillCatalogError,
    SkillResourceRequest,
    StructuredResponseError,
)

from pydantic import BaseModel as _PydanticBaseModel

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.orchestration.hypothesis import (
        AttemptState,
        CarryOver,
        HypothesisSearch,
        HypothesisState,
    )
    from vibesys.orchestration.hypothesis.state import Hypothesis, RoundRecord
    from vibesys.orchestration.multi.files import MultiFiles
    from vibesys.orchestration.multi.models import MultiOptions, ProfileGuidedMultiOptions
    from vibesys.orchestration.profile_focus import FocusView
    from vs_runtime.api import AgentBinding, AgentSession, Workspace


@dataclass(frozen=True, slots=True)
class PlanRequest:
    """Evidence supplied to one independent designer plan conversation."""

    round_number: int
    state: HypothesisState
    carry: CarryOver
    profiler_summary: ProfilerSummary | None
    plateau_warning: str | None
    provisional_candidates: int
    workspace: Workspace
    guidance: FocusView | None


@dataclass(frozen=True, slots=True)
class AttemptRequest:
    """Multi policy facts shared by one implementer and its judge."""

    round_number: int
    plan: OrchestratorPlan
    planned_official_reason: str | None
    records: tuple[RoundRecord, ...]
    active_hypothesis: Hypothesis
    workspace: Workspace
    guidance: FocusView | None


def _fallback_pre_round() -> PreRoundDecision:
    return PreRoundDecision(
        need_profile=False,
        profile_focus="",
        reasoning="fallback: default to skip",
    )


def _fallback_plan() -> OrchestratorPlan:
    return OrchestratorPlan.model_validate(
        {
            "task": "Re-check minimal server boots and /health returns 200.",
            "pass_criteria": "/health returns 200.",
            "reasoning": "fallback: orchestrator produced no structured response",
        }
    )


def _fallback_profiler() -> ProfilerSummary:
    return ProfilerSummary(
        analysis="Profiler produced no structured response.",
        bottlenecks="n/a",
        suggestions="Re-run profiling on the next round.",
    )


def _fallback_implementer() -> ImplementerResponse:
    return ImplementerResponse(
        summary="Implementer produced no structured response.",
        expected_behavior="unknown",
        hypothesis_outcome="inconclusive",
        evidence="The implementer output could not be parsed.",
        next_step="Recover retained evidence and return a schema-valid response before review.",
    )


def _timeout_implementer(timeout_seconds: float) -> ImplementerResponse:
    return ImplementerResponse(
        summary="Implementer invocation timed out.",
        expected_behavior="unknown",
        hypothesis_outcome="inconclusive",
        evidence=(
            f"The framework stopped the implementer after {timeout_seconds:g} seconds "
            "without a structured response."
        ),
        next_step="Inspect retained evidence and return a schema-valid response on retry.",
    )


def _fallback_judge() -> JudgeResponse:
    return JudgeResponse(
        analysis="Judge produced no structured response.",
        feedback="No structured response received.",
        verdict=Verdict.FAIL,
    )


async def _resolve_skills(
    run: Run,
    selections: list[SkillResourceSelection],
) -> tuple[list[SkillResourceSelection], list[ResolvedSkillResources]]:
    if not selections:
        return [], []
    requests = tuple(
        SkillResourceRequest(
            name=item.skill,
            resource_paths=tuple(item.resource_paths),
            purpose=item.purpose,
        )
        for item in selections
    )
    try:
        result = await run.skills.resolve(requests)
    except SkillCatalogError as error:
        run.observations.warning(
            f"[skills] ignored recommendations because the catalog is invalid: {error}"
        )
        return [], []
    for diagnostic in result.diagnostics:
        run.observations.warning(f"[skills] {diagnostic}")
    portable = [
        SkillResourceSelection(
            skill=item.name,
            resource_paths=[path.removeprefix(f"{item.name}/") for path in item.resource_paths],
            purpose=item.purpose,
        )
        for item in result.resolved
    ]
    return portable, list(result.resolved)


# When this variable is set, each new plan is a draft: one fresh implementer-role session
# runs the workspace's microbench phase on the draft, then the same orchestrator
# conversation revises the plan from the measured answers. Only the revised plan reaches
# the implementer.
MICROBENCH_PHASE_ENV = "VIBESYS_MICROBENCH_PHASE"
MICROBENCH_MINUTES_ENV = "VIBESYS_MICROBENCH_MINUTES"
# Rounds up to this number run the full microbench phase. A later round runs it only for
# the open questions of its draft that a timeline of the current kernel cannot settle, and
# skips it when there are none. Unset: every round runs the full phase.
MICROBENCH_FULL_ROUNDS_ENV = "VIBESYS_MICROBENCH_FULL_ROUNDS"
# When set, a round past the full microbench rounds (two when the count is unset) is a phase
# round: the implementer coordinates one subagent per target phase of the plan.
PHASE_AGENTS_ENV = "VIBESYS_PHASE_AGENTS"

_PHASE_ROUND_IMPLEMENTER = """

This is a phase round. Follow the "Phase table and phase rounds" section of the objective
(`{objective}`): give each target phase of the plan to a `phase` subagent, all at once, and
coordinate them. Do not edit phase code yourself before they return. Then merge, check and
time the merged build as that section says.
"""

_PHASE_ROUND_PLAN = """

This is a phase round: name the target phases and the boundary items of the plan from the
phase windows of the latest timeline, largest gaps first.
"""

MEASUREMENT_DIR_ENV = "VIBESYS_MEASUREMENT_DIR"


def _phase_round(round_number: int) -> bool:
    """True when phase agents are on and this round is past the full microbench rounds."""
    if not os.environ.get(PHASE_AGENTS_ENV):
        return False
    raw = os.environ.get(MICROBENCH_FULL_ROUNDS_ENV, "").strip()
    try:
        return round_number > (int(raw) if raw else 2)
    except ValueError:
        return False

_MEASURE_BEFORE_RETURN = """Your turn returned `continue`, but no new measurement file appeared in `{directory}`
during this round. A round that ends without a measurement gives the next round nothing to
act on. If a GPU command that you started is still running, wait for it in the foreground
and read its result. If no measurement was started, run the check now. Then return the
structured response again, with the measured outcome.
"""


def _measurements(root: object, directory: str) -> set[str]:
    """Names of the files under *directory* of the workspace at *root*."""
    base = Path(str(root)) / directory
    if not base.is_dir():
        return set()
    return {str(path.relative_to(base)) for path in base.rglob("*")}


_MICROBENCH_DEADLINE = """
The phase has a time limit. Write `deadline: {deadline}` as the first line of the request
file, so that the subagent fits its measurements to it. At the deadline, the answers settle
what the rows settle, and the rest stays open in the ledger for a later round.
"""

_MICROBENCH_ONLY = """
This round measures only the open questions below. The timeline of the current kernel and
the existing rows settle the other choices of the draft, so do not request benches for them.

{questions}
"""

_MICROBENCH_PROMPT = """You run the microbench phase of round {round_name} before its plan is final.
The orchestrator wrote a draft plan at `{plan_location}`. The implementer will not see it:
the orchestrator revises it from your answers, and the implementer builds the revised plan.

Do steps 1-3 of the Microbench phase section of the objective (`{objective}`) for this draft:
run `./microbench_items`, write `progress/microbench/requests/{round_name}.md`, and give it to
the `microbench` subagent (Agent tool, `subagent_type: microbench`, `run_in_background:
false`). Ask for each choice that the draft makes or rejects and that a measurement could
change: the engine, decomposition and data path of each phase it writes, and the costs it
assumes. Then run `./microbench_check`. If it fails, give its message to the subagent and
wait again, until it passes.

Do not edit the submission or any file outside `progress/`. Return a short summary: for each
choice of the draft, what the answers measured and whether they confirm or contradict it,
with row ids.
"""

_REVISE_PROMPT = """The microbench phase measured the questions of your draft plan for round
{round_name}. Its summary:

{summary}

Read `progress/microbench/answers/{round_name}.md`, `progress/microbench/open.md`, the
`{round_name}` status entry of `progress/engine-map.md` and `progress/design-space.md`.
Then revise the plan. For each choice of the draft (engine, decomposition, data path,
target), keep it, change it or replace it as the measured rows decide, and cite the row ids
in the plan. If the rows show a larger gain than the draft's target, retarget the round.
The implementer receives only the revised plan and builds what it says.
Name one design for each phase that the round writes: the design that the matched rows rank
fastest, with no fallback to a design that they rank slower and no condition on how hard it
is to build. A measurement of the phase inside the candidate kernel ranks designs above the
proxy rows: where the per-phase timeline of the parent shows that the design the rows prefer
is slower inside the kernel, name the design that the timeline shows faster, cite both
measurements, and keep the gap between proxy and kernel open in the microbench ledger.

Return the complete plan for this round as the same JSON object. You may keep the
hypothesis_id of the draft.
"""


_OPEN_QUESTIONS_PROMPT = """Before the microbench phase of round {round_name}: the opening rounds
measured the engine, decomposition and data-path choices. From this round on, the per-phase
timeline of the current kernel (`./timeline_candidate`) is the default evidence, and the
microbench phase runs only for questions that the timeline cannot settle.

Read `progress/microbench/open.md` and the latest timeline in `reports/`. List the choices of
your draft that depend on a measurement that neither the timeline of the current kernel nor
existing rows (`./primdb`) gives: for example a design that the kernel does not contain yet
and that no matched row prices. For each, say what measurement would settle it and why the
timeline cannot. Return an empty list when the timeline and the existing rows settle every
choice of the draft.
"""


class OpenQuestionsResponse(_PydanticBaseModel):
    """Questions of a draft plan that only a new microbenchmark can settle."""

    questions: list[str]
    reason: str


class MicrobenchPhaseResponse(_PydanticBaseModel):
    """Summary of one pre-plan microbench phase."""

    summary: str


class MultiAgentTurns:
    """Render policy prompts and own the explicit conversation topology."""

    def __init__(
        self,
        run: Run,
        options: MultiOptions | ProfileGuidedMultiOptions,
        search: HypothesisSearch,
        files: MultiFiles,
    ) -> None:
        """Bind run effects and pure policy without opening conversations."""
        self.run = run
        self.options = options
        self.search = search
        self.files = files
        self.workspace = run.workspaces.root
        self.domain = resolve_domain(DomainName(run.facts.domain_id))
        self.modality = options.modality
        if self.modality is None and self.domain.name is DomainName.LLM_SERVING:
            self.modality = "text_generation"
        self._workers: dict[str, AgentSession] = {}
        self._closed = False

    def _domain_context(self, *, trusted_commands: bool = True) -> dict[str, object]:
        facts = self.run.facts
        return {
            "modality": self.modality,
            "interface": self.options.interface,
            "reference_path": facts.reference_location,
            "benchmark_command": facts.benchmark_command if trusted_commands else None,
            "accuracy_command": facts.accuracy_command if trusted_commands else None,
            "runtime_notes": facts.environment_notes,
            "profile_execution": facts.profile_execution.value,
            "workspace_sources": tuple(item.model_dump() for item in facts.workspace_sources),
        }

    async def pre_round(
        self,
        round_number: int,
        carry: CarryOver,
        *,
        has_history: bool,
    ) -> PreRoundDecision:
        """Ask a fresh designer conversation whether specialist evidence is useful."""
        facts = self.run.facts
        context = PreRoundContext(
            objective_location=facts.objective_location,
            regression_info=carry.regression_info,
            exhaustion_info=carry.exhaustion_info,
            progress_location=self.files.progress_location,
            profiler_kind=facts.profiler_id,
            profile_execution=facts.profile_execution.value,
            has_history=has_history,
        )
        session = await self.run.agents.create_session(
            DESIGNER,
            workspace=self.workspace,
            writable_paths=(self.files.roadmap_location,),
        )
        try:
            try:
                decision = await session.turn(
                    render_pre_round_prompt(context), response=PreRoundDecision
                )
            except StructuredResponseError:
                decision = _fallback_pre_round()
        finally:
            await session.close()
        self.files.note_pre_round(round_number, decision)
        return decision

    def _plan_context(self, request: PlanRequest) -> PlanContext:
        facts = self.run.facts
        return PlanContext(
            objective_location=facts.objective_location,
            profiler_summary=request.profiler_summary,
            regression_info=request.carry.regression_info,
            exhaustion_info=request.carry.exhaustion_info,
            progress_location=self.files.progress_location,
            roadmap_location=self.files.roadmap_location,
            pareto_archive_location=self.files.pareto_location,
            plateau_warning=request.plateau_warning,
            domain_orchestrator=render_domain_section(
                self.domain,
                DomainRole.ORCHESTRATOR,
                **self._domain_context(),
            ),
            runtime_notes=facts.environment_notes,
            framework_benchmark_enabled=facts.benchmark_configured,
            official_eval_every=self.options.official_eval_every,
            provisional_candidates=request.provisional_candidates,
            official_eval_cadence_due=(
                request.provisional_candidates + 1 >= self.options.official_eval_every
            ),
            active_component=(
                request.guidance.active_component if request.guidance is not None else None
            ),
            ledger_text=(request.guidance.ledger_text if request.guidance is not None else None),
            ranked_bottlenecks=(
                [
                    {
                        "component": item.name,
                        "cost_share": item.share * 100,
                        "evidence": item.evidence,
                    }
                    for item in request.guidance.ranked_bottlenecks
                ]
                if request.guidance is not None
                else []
            ),
        )

    def _validate_plan(self, plan: OrchestratorPlan, state: HypothesisState) -> None:
        updates = [item.hypothesis_id for item in plan.hypothesis_updates]
        if len(updates) != len(set(updates)):
            raise InvalidPlanError.duplicate_updates()
        if plan.hypothesis_id in updates:
            raise InvalidPlanError.self_reference()
        if state.by_id(plan.hypothesis_id) is not None:
            raise InvalidPlanError.reused_id(plan.hypothesis_id)
        self.search.validate_updates(state, plan.hypothesis_updates)

    async def plan(self, request: PlanRequest) -> OrchestratorPlan:
        """Return one normalized, state-valid plan after at most one correction.

        With MICROBENCH_PHASE_ENV set, that plan is a draft: the microbench phase measures
        it, and the same conversation returns the revised plan.
        """
        context = self._plan_context(request)
        session = await self.run.agents.create_session(
            DESIGNER,
            workspace=request.workspace,
            writable_paths=(self.files.roadmap_location,),
        )
        try:
            try:
                prompt = render_plan_prompt(context)
                if _phase_round(request.round_number):
                    prompt += _PHASE_ROUND_PLAN
                plan = await session.turn(prompt, response=OrchestratorPlan)
            except StructuredResponseError:
                plan = _fallback_plan()
            plan = await self._settle_plan(session, plan, request)
            if not os.environ.get(MICROBENCH_PHASE_ENV):
                return plan
            round_name = f"round-{request.round_number:04d}"
            questions: list[str] | None = None
            if self._microbench_on_demand(request.round_number):
                questions, reason = await self._open_questions(session, round_name)
                if not questions:
                    self.files.write_plan(request.round_number, plan)
                    skipped = self.files.artifact_root / "microbench" / "skipped" / f"{round_name}.md"
                    skipped.parent.mkdir(parents=True, exist_ok=True)
                    skipped.write_text(
                        f"# {round_name}: microbench phase skipped\n\n"
                        "The planning session found no open question of the draft that the "
                        "timeline of the current kernel and the existing rows do not settle.\n\n"
                        f"{reason.strip()}\n")
                    self.run.observations.note(
                        f"[microbench] {round_name}: no open question needs a new "
                        "microbenchmark; skipping the phase")
                    return plan
            summary = await self._microbench_phase(request, plan, round_name, questions)
            self.run.observations.note(f"[microbench] {round_name} draft measured; revising plan")
            try:
                revised = await session.turn(
                    _REVISE_PROMPT.format(round_name=round_name, summary=summary),
                    response=OrchestratorPlan,
                )
            except StructuredResponseError:
                self.run.observations.warning("[microbench] no revised plan; keeping the draft")
                return plan
            try:
                return await self._settle_plan(session, revised, request)
            except ValueError as error:
                self.run.observations.warning(
                    f"[microbench] revised plan rejected ({error}); keeping the draft")
                self.files.write_plan(request.round_number, plan)
                return plan
        finally:
            await session.close()

    @staticmethod
    def _microbench_on_demand(round_number: int) -> bool:
        """True when this round is past the rounds that run the full microbench phase."""
        raw = os.environ.get(MICROBENCH_FULL_ROUNDS_ENV, "").strip()
        if not raw:
            return False
        try:
            return round_number > int(raw)
        except ValueError:
            return False

    async def _open_questions(
        self, session: AgentSession, round_name: str
    ) -> tuple[list[str], str]:
        """Ask the planning session which questions of its draft need a new microbenchmark."""
        try:
            response = await session.turn(
                _OPEN_QUESTIONS_PROMPT.format(round_name=round_name),
                response=OpenQuestionsResponse,
            )
        except StructuredResponseError:
            self.run.observations.warning(
                f"[microbench] {round_name}: no open-question list; running the full phase")
            return ["(no list returned: measure the choices of the draft)"], ""
        return [q for q in response.questions if q.strip()], response.reason

    async def _microbench_phase(
        self,
        request: PlanRequest,
        request_plan: OrchestratorPlan,
        round_name: str,
        questions: list[str] | None = None,
    ) -> str:
        """Run the workspace's microbench phase on the draft plan in a fresh session."""
        plan_location = self.files.write_plan(request.round_number, request_plan)
        # The gate tells the draft's claims from claims that only the revised plan makes.
        draft = self.files.artifact_root / "microbench" / "draft" / f"{round_name}.json"
        draft.parent.mkdir(parents=True, exist_ok=True)
        written = self.files.artifact_root / "plans" / f"{round_name}.json"
        draft.write_text(written.read_text() if written.is_file()
                         else request_plan.model_dump_json())
        prompt = _MICROBENCH_PROMPT.format(
            round_name=round_name,
            plan_location=plan_location,
            objective=self.run.facts.objective_location,
        )
        if questions is not None:
            prompt += _MICROBENCH_ONLY.format(
                questions="\n".join(f"- {q}" for q in questions))
        minutes = os.environ.get(MICROBENCH_MINUTES_ENV, "").strip()
        hard_limit: float | None = None
        if minutes:
            end = datetime.now(timezone.utc) + timedelta(minutes=float(minutes))
            prompt += _MICROBENCH_DEADLINE.format(
                deadline=end.strftime("%Y-%m-%dT%H:%M UTC"))
            # The deadline in the prompt is advisory; this one is not. The grace lets the
            # agents write the answers they have.
            hard_limit = 60 * (float(minutes) + max(5.0, 0.25 * float(minutes)))
        session = await self.run.agents.create_session(
            IMPLEMENTER,
            workspace=request.workspace,
            member_id=f"microbench-{request.round_number:04d}",
        )
        try:
            try:
                response = await asyncio.wait_for(
                    session.turn(prompt, response=MicrobenchPhaseResponse), hard_limit)
                return response.summary
            except StructuredResponseError:
                self._mark_stopped(round_name, "returned no structured summary")
                return "The microbench phase returned no summary; read its answer file."
            except asyncio.TimeoutError:
                marker = self.files.artifact_root / "microbench" / "deadline" / f"{round_name}.md"
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(
                    f"# {round_name}: microbench phase stopped at its hard time limit\n\n"
                    "Plan claims and carried questions without answers stay open in the "
                    "ledger for a later round.\n")
                self.run.observations.warning(
                    f"[microbench] {round_name}: hard time limit "
                    f"({hard_limit / 60:.0f} min) reached; the plan is revised from the "
                    "answers written so far")
                return ("The microbench phase reached its hard time limit and was stopped. "
                        f"Read `progress/microbench/answers/{round_name}.md` for what it "
                        "measured; questions without answers stay open in the ledger.")
        except AgentUsageLimitError:
            raise
        except Exception as error:  # noqa: BLE001  # the draft still stands if measuring fails
            self.run.observations.warning(f"[microbench] phase failed: {error}")
            self._mark_stopped(round_name, f"failed ({error})")
            return f"The microbench phase failed ({error}); read whatever answers exist."
        finally:
            await session.close()

    def _mark_stopped(self, round_name: str, how: str) -> None:
        """Without its answers file, a phase that ended early must not block the round's gate."""
        root = self.files.artifact_root / "microbench"
        if (root / "answers" / f"{round_name}.md").is_file():
            return
        marker = root / "stopped" / f"{round_name}.md"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            f"# {round_name}: microbench phase {how} before it wrote its answers\n\n"
            "Plan claims and carried questions stay open in the ledger for a later round.\n")

    async def _settle_plan(
        self, session: AgentSession, plan: OrchestratorPlan, request: PlanRequest
    ) -> OrchestratorPlan:
        """Normalize and validate *plan*, reprompting once; persist and return it."""
        for attempt in range(2):
            plan.hypothesis_id = (
                plan.hypothesis_id.strip() or f"hypothesis-{request.round_number:04d}"
            )
            plan.title = normalize_hypothesis_title(plan.title)
            try:
                self._validate_plan(plan, request.state)
            except ValueError as error:
                if attempt:
                    raise
                rejected = ", ".join(
                    sorted({item.hypothesis_id for item in plan.hypothesis_updates})
                )
                feedback = (
                    f"Your previous plan was rejected: {error}. It proposed "
                    f"hypothesis_id {plan.hypothesis_id!r} and named "
                    f"{rejected or '(no)'} in hypothesis_updates. A hypothesis_id "
                    "names one investigation permanently: never reuse an identifier "
                    "used earlier in this run. hypothesis_updates may name each prior "
                    "hypothesis at most once, and never the new one. Produce a corrected "
                    "plan for this round. Return only the JSON object."
                )
                self.run.observations.warning(
                    f"[orchestrator] plan rejected ({error}); reprompting once"
                )
                try:
                    plan = await session.turn(feedback, response=OrchestratorPlan)
                except StructuredResponseError:
                    plan = _fallback_plan()
                continue
            portable, _ = await _resolve_skills(self.run, plan.recommended_skills)
            plan.recommended_skills = portable
            self.files.write_plan(request.round_number, plan)
            self.files.note_plan(request.round_number, plan)
            return plan
        message = "designer correction loop exited without a validated plan"
        raise RuntimeError(message)

    def _profiler_campaign_context(self, artifact: str) -> str:
        return f"""

## Recent campaign context

The durable progress artifact is `{self.files.progress_location}`. Inspect the most
recent applicable round before older evidence. Write bounded durable profile
evidence only below `{artifact}` and keep large transient traces under `/tmp`.

## Read-only evidence boundary

Never edit candidate source, configuration, tests, locks, instrumentation,
endpoints, or entrypoints. Report an observability mismatch when the configured
production path cannot be measured safely.
"""

    async def profile(self, round_number: int, focus: str) -> ProfilerSummary | None:
        """Collect optional evidence in one fresh bounded-write conversation."""
        facts = self.run.facts
        kind = ProfilerKind(facts.profiler_id)
        if kind is ProfilerKind.NONE:
            return None
        definition = profiler_definition(kind)
        if definition.requires_domain_torch_support and not self.domain.supports_torch_profiler:
            raise UnsupportedProfilerError
        artifact = self.files.profiler_location(round_number).rstrip("/")
        context = ProfilerContext(
            profile_focus=focus,
            benchmark_command=facts.benchmark_command,
            modality=self.modality,
            domain_profiler=render_domain_section(
                self.domain,
                DomainRole.PROFILER,
                **self._domain_context(),
            ),
            runtime_notes=facts.environment_notes,
            profile_execution=facts.profile_execution.value,
            objective=None,
            profiler_support_name=definition.support_name,
            profiler_mcp_name=definition.mcp_name,
            profiler_campaign_context=self._profiler_campaign_context(artifact),
        )
        session = await self.run.agents.create_session(
            PROFILER,
            workspace=self.workspace,
            writable_paths=(artifact,),
        )
        try:
            try:
                summary = await session.turn(
                    render_profiler_prompt(kind.value, context),
                    response=ProfilerSummary,
                )
            except StructuredResponseError:
                summary = _fallback_profiler()
        except AgentUsageLimitError:
            raise
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-920440 [BLE001]; profiling is advisory; a failed specialist must not abort the policy round.
            self.run.observations.warning(f"[profiler] failed: {error}")
            return None
        finally:
            await session.close()
        self.files.note_profile(round_number, summary)
        return summary

    async def _implementer_context(
        self,
        request: AttemptRequest,
        state: AttemptState,
    ) -> ImplementerContext:
        plan = request.plan
        hypothesis = request.active_hypothesis
        plan.recommended_skills, resolved = await _resolve_skills(self.run, plan.recommended_skills)
        plan_location = self.files.write_plan(request.round_number, plan)
        facts = self.run.facts
        return ImplementerContext(
            reference_path=facts.reference_location,
            modality=self.modality,
            interface=self.options.interface,
            domain_implementer=render_domain_section(
                self.domain,
                DomainRole.IMPLEMENTER,
                **self._domain_context(),
            ),
            objective_location=facts.objective_location,
            plan_artifact_location=plan_location,
            progress_location=self.files.progress_location,
            pareto_archive_location=self.files.pareto_location,
            validation_location=self.files.validation_location,
            validation_recipe_contract_location=self.files.validation_schema_location,
            retry=state.retry,
            feedback=state.feedback,
            framework_revert_applied=hypothesis.revert_applied,
            framework_revert_round=hypothesis.parent_round,
            framework_revert_commit=hypothesis.revert_commit,
            gate_revalidation_pending=hypothesis.gate_revalidation_pending,
            gate_approved_perf_metric=hypothesis.gate_approved_perf_metric,
            gate_approved_perf_unit=hypothesis.gate_approved_perf_unit,
            gate_approved_evaluation_artifact=hypothesis.gate_approved_evaluation_artifact,
            runtime_notes=facts.environment_notes,
            framework_benchmark_enabled=facts.benchmark_configured,
            official_evaluation_due=request.planned_official_reason is not None,
            official_evaluation_reason=request.planned_official_reason,
            recommended_skills=resolved,
            prior_attempt_artifact_locations=self.files.prior_implementer_locations(
                request.round_number
            ),
            active_component=(
                request.guidance.active_component if request.guidance is not None else None
            ),
        )

    async def _continuation_context(
        self,
        request: AttemptRequest,
        state: AttemptState,
    ) -> ImplementerContinuationContext:
        plan = request.plan
        hypothesis = request.active_hypothesis
        plan.recommended_skills, resolved = await _resolve_skills(self.run, plan.recommended_skills)
        plan_location = self.files.write_plan(request.round_number, plan)
        return ImplementerContinuationContext(
            hypothesis_id=plan.hypothesis_id,
            objective_location=self.run.facts.objective_location,
            plan_artifact_location=plan_location,
            progress_location=self.files.progress_location,
            pareto_archive_location=self.files.pareto_location,
            validation_location=self.files.validation_location,
            validation_recipe_contract_location=self.files.validation_schema_location,
            runtime_notes=self.run.facts.environment_notes,
            prior_attempt_artifact_locations=self.files.prior_implementer_locations(
                request.round_number
            ),
            retry=state.retry,
            continuation_step=hypothesis.next_step or plan.task,
            feedback=state.feedback,
            recommended_skills=resolved,
            framework_revert_applied=hypothesis.revert_applied,
            framework_revert_round=hypothesis.parent_round,
            framework_revert_commit=hypothesis.revert_commit,
            gate_revalidation_pending=hypothesis.gate_revalidation_pending,
            gate_approved_evaluation_artifact=hypothesis.gate_approved_evaluation_artifact,
        )

    async def implement(
        self,
        request: AttemptRequest,
        state: AttemptState,
    ) -> tuple[ImplementerResponse, bool]:
        """Run one attempt in the hypothesis's context-preserving conversation."""
        if self._closed:
            message = "multi-agent turns are closed"
            raise RuntimeError(message)
        plan = request.plan
        continuing = bool(request.active_hypothesis.next_step)
        context: BaseModel = (
            await self._continuation_context(request, state)
            if continuing
            else await self._implementer_context(request, state)
        )
        session = self._workers.get(plan.hypothesis_id)
        if session is None:
            session = await self.run.agents.create_session(
                IMPLEMENTER,
                workspace=request.workspace,
                member_id=plan.hypothesis_id,
            )
            self._workers[plan.hypothesis_id] = session
        elif session.workspace != request.workspace:
            message = f"hypothesis {plan.hypothesis_id!r} changed workspace"
            raise ValueError(message)
        prompt = (
            render_continuation_prompt(context)
            if isinstance(context, ImplementerContinuationContext)
            else render_implementer_prompt(context)
        )
        if _phase_round(request.round_number):
            prompt += _PHASE_ROUND_IMPLEMENTER.format(
                objective=self.run.facts.objective_location)
        directory = os.environ.get(MEASUREMENT_DIR_ENV, "").strip()
        root = getattr(request.workspace, "path", None)
        before = _measurements(root, directory) if directory and root is not None else None
        try:
            response = await session.turn(prompt, response=ImplementerResponse)
            if (
                before is not None
                and response.hypothesis_outcome == HypothesisOutcome.CONTINUE
                and _measurements(root, directory) <= before
            ):
                self.run.observations.warning(
                    "[implementer] continue without a new measurement; resuming once")
                response = await session.turn(
                    _MEASURE_BEFORE_RETURN.format(directory=directory),
                    response=ImplementerResponse,
                )
        except StructuredResponseError:
            response = _fallback_implementer()
        except AgentTurnTimeoutError as error:
            response = _timeout_implementer(error.timeout_seconds)
        synthesized = response.summary in {
            "Implementer produced no structured response.",
            "Implementer invocation timed out.",
        }
        updates, _ = await _resolve_skills(self.run, response.skill_context_updates)
        if updates:
            plan.recommended_skills, _ = await _resolve_skills(
                self.run,
                [*plan.recommended_skills, *updates],
            )
            self.files.write_plan(request.round_number, plan)
        self.files.write_implementer(request.round_number, state.retry, response)
        self.files.note_implementation(request.round_number, state.retry, response)
        return response, synthesized

    async def review(
        self,
        request: AttemptRequest,
        state: AttemptState,
        conflict: str | None,
    ) -> JudgeResponse:
        """Audit one implementation using a fresh read-only conversation."""
        implementation = state.implementation
        if implementation is None:
            message = "judge requires an implementer response"
            raise RuntimeError(message)
        facts = self.run.facts
        plan_location = self.files.write_plan(request.round_number, request.plan)
        evidence_location = self.files.write_implementer(
            request.round_number,
            state.retry,
            implementation,
        )
        context = JudgeContext(
            domain_judge=render_domain_section(
                self.domain,
                DomainRole.JUDGE,
                **self._domain_context(trusted_commands=False),
            ),
            framework_benchmark_enabled=facts.benchmark_configured,
            framework_revert_applied=request.active_hypothesis.revert_applied,
            framework_revert_round=request.active_hypothesis.parent_round,
            framework_revert_commit=request.active_hypothesis.revert_commit,
            gate_approved_evaluation_artifact=(
                request.active_hypothesis.gate_approved_evaluation_artifact
            ),
            gate_approved_perf_metric=request.active_hypothesis.gate_approved_perf_metric,
            gate_approved_perf_unit=request.active_hypothesis.gate_approved_perf_unit,
            gate_revalidation_pending=request.active_hypothesis.gate_revalidation_pending,
            implementer_artifact_location=evidence_location,
            interface=self.options.interface,
            modality=self.modality,
            objective_location=facts.objective_location,
            official_evaluation_due=request.planned_official_reason is not None,
            official_evaluation_reason=request.planned_official_reason,
            pareto_archive_conflict=conflict,
            pareto_archive_location=self.files.pareto_location,
            plan_artifact_location=plan_location,
            progress_location=self.files.progress_location,
            retry=state.retry,
            runtime_notes=facts.environment_notes,
            validation_location=self.files.validation_location,
            validation_recipe_contract_location=self.files.validation_schema_location,
        )
        session = await self.run.agents.create_session(JUDGE, workspace=request.workspace)
        try:
            try:
                response = await session.turn(
                    render_judge_prompt(context),
                    response=JudgeResponse,
                )
            except StructuredResponseError:
                response = _fallback_judge()
        finally:
            await session.close()
        if response.verdict is Verdict.PASS and conflict:
            response = response.model_copy(
                update={
                    "analysis": f"{response.analysis}\n\nFramework Pareto guard: {conflict}",
                    "feedback": conflict,
                    "verdict": Verdict.FAIL,
                }
            )
        self.files.note_judge(request.round_number, state.retry, response)
        return response

    def binding(self, hypothesis_id: str) -> AgentBinding:
        """Return runtime attribution for a hypothesis whose work started."""
        try:
            return self._workers[hypothesis_id].binding
        except KeyError as error:
            message = f"hypothesis {hypothesis_id!r} has no implementer session"
            raise ValueError(message) from error

    async def close(self) -> None:
        """Release every named implementer conversation once."""
        if self._closed:
            return
        self._closed = True
        for session in reversed(tuple(self._workers.values())):
            await session.close()


__all__ = ["AttemptRequest", "MultiAgentTurns", "PlanRequest"]
