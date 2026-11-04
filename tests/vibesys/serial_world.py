"""The serial searches' world on the production shell: what their agents reply and measure.

`SerialWorld` is a vs-dst `World` for the single or the multi search: the search, its config
and the scripted far side of its run, on the same executors as the dynamic world (workspaces,
sessions, measurements, the evaluator). A turn's reply is chosen by its reply schema, since the
multi search's designer and router share a role. Every designer reply names a new hypothesis.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from tests.support.dynamic_script import FACTS, TERMS, Executors, lease
from tests.vibesys.serial_search._replies import plan_reply

from vibesys.candidates.api import RunBudget, limits_for
from vibesys.orchestration.multi.core_policy.api import requirements_for as multi_requirements
from vibesys.orchestration.multi.search import api as multi_api
from vibesys.orchestration.single.core_policy.api import requirements_for as single_requirements
from vibesys.orchestration.single.search import api as single_api
from vibesys.serial_roadmap import RoadmapOwner
from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    CaptureCommandBase,
    CommandCapture,
    DispatchTurn,
    ExecuteRegisteredOperation,
    FlowState,
    InterpretEvidenceBase,
    LocalValidationReport,
    RenderRoleArtifactsBase,
    ResumeSessionTurn,
    RunEnvelope,
    ValidateLocalBase,
)
from vs_core.testing.builders import sample_retries
from vs_core.testing.drive import Answer, Harness, Succeeded
from vs_dst.api import RunSetup
from vs_issue_tracker.api import FakeRoadmap, PutRoadmapItems, ReadRoadmap
from vs_runtime.api.testing import Launch

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

    from vs_core.api import (
        CoreState,
        EvidenceRequirements,
        OperationRegistry,
        Request,
        SearchDef,
        StrategyOperations,
        TaskTerms,
    )

type Search = Literal["single", "multi"]


class _Bounds:
    """A backend whose requests are retried twice and polled quickly (as the dynamic world's)."""

    request_retries = sample_retries(2)
    observe_interval_seconds = 1.0
    observe_backoff_cap_seconds = 1.0
    cancellation_bound_seconds = 60.0
    cleanup_bound_seconds = 60.0
    reconciliation_bound_seconds = 5000.0
    queue_wait_budget_seconds = 43_200.0
    queue_notice_interval_seconds = 600.0


_IMPLEMENTED = {"summary": "did it", "expected_behavior": "faster"}
_REPLIES: dict[Search, dict[str, str]] = {
    "single": {
        "agent.implementer.reply": json.dumps(
            {
                **_IMPLEMENTED,
                "self_review": "ok",
                "feedback": "",
                "verdict": "pass",
                "bottlenecks": "",
                "suggestions": "",
                "profile_analysis": "",
            }
        ),
    },
    "multi": {
        "agent.implementer.reply": json.dumps({**_IMPLEMENTED, "status": "implemented"}),
        "agent.judge.reply": json.dumps({"analysis": "checked", "feedback": "", "verdict": "pass"}),
        "agent.router.reply": json.dumps(
            {"need_profile": False, "profile_focus": "", "reasoning": "because"}
        ),
        "agent.profiler.reply": json.dumps(
            {"analysis": "analysis", "bottlenecks": "hot loop", "suggestions": "fuse it"}
        ),
    },
}
"""What each role of a search replies, by reply schema; the planner's reply is drawn fresh."""


@dataclass
class SerialExecutors(Executors):
    """The dynamic script's executors, answering a serial search's roles and operations."""

    search: Search = "single"
    designs: int = 0
    roadmap: FakeRoadmap = field(default_factory=FakeRoadmap)
    """The run's roadmap, kept outside the run like its store, so it outlives a crash."""

    def _turn(self, request: DispatchTurn | ResumeSessionTurn) -> Answer:
        schema = request.turn.output_schema.name
        if schema == "agent.orchestrator.reply":
            self.designs += 1
            reply = plan_reply(f"h{self.designs}")
        else:
            reply = _REPLIES[self.search][schema]
        return Succeeded(output_json=reply, resource_id=lease(request.turn.session.session_id))

    def _operation(self, request: ExecuteRegisteredOperation) -> Answer:
        typed = _SEARCHES[self.search].registry().decode(request.operation)
        outcome: BaseModel
        if isinstance(typed, ReadRoadmap | PutRoadmapItems):
            outcome = RoadmapOwner(self.roadmap).perform(typed)
        elif isinstance(typed, RenderRoleArtifactsBase):
            prompt = ArtifactRef(
                artifact_id=ArtifactId(root=f"prompt:{request.operation_id.root}"), digest="prompt"
            )
            outcome = typed.outcome_model.model_validate(
                {"status": "succeeded", "prompts": (prompt,)}
            )
        elif isinstance(typed, InterpretEvidenceBase):
            outcome = typed.outcome_model.model_validate(
                {
                    "status": "succeeded",
                    "readings": tuple(self.readings[item.evidence_id] for item in typed.evidence),
                }
            )
        elif isinstance(typed, CaptureCommandBase):
            outcome = CommandCapture(status="succeeded", exit_code=0, output="")
        elif isinstance(typed, ValidateLocalBase):
            outcome = LocalValidationReport(status="succeeded", passed=True)
        else:
            outcome = typed.outcome_model.model_validate({"status": "succeeded", "verified": True})
        return Succeeded(outcome=outcome)


@dataclass(frozen=True)
class _Kind:
    """What the world needs of one serial search."""

    definition: SearchDef[Any]
    config: type[BaseModel]
    operations: StrategyOperations
    budget: Callable[[Any], RunBudget]
    requirements: Callable[[TaskTerms], EvidenceRequirements]

    def registry(self) -> OperationRegistry:
        """The codec of every operation the search records: its kit's and the roadmap's."""
        registry = self.definition.registry()
        assert registry is not None
        return registry


_SEARCHES: dict[Search, _Kind] = {
    "single": _Kind(
        single_api.single,
        single_api.SingleConfig,
        single_api.OPERATIONS,
        single_api.budget,
        single_requirements,
    ),
    "multi": _Kind(
        multi_api.multi,
        multi_api.MultiConfig,
        multi_api.OPERATIONS,
        multi_api.budget,
        multi_requirements,
    ),
}


class SerialWorld:
    """The planner, implementer (and for multi the router, profiler and judge) of one run."""

    def __init__(
        self,
        search: Search = "single",
        *,
        throughputs: tuple[float | None, ...] = (100.0,),
        **settings: object,
    ) -> None:
        """Script a run of ``search``; any other option is a field of the search's config.

        ``throughputs`` is what candidates measure (None: the benchmark fails), picked by a
        candidate's commit. The run is one round unless ``max_rounds`` says otherwise.
        """
        kind = _SEARCHES[search]
        selected = kind.config.model_validate({"max_rounds": 1, **settings})
        self.config = selected
        self.setup = RunSetup(
            new_launch=lambda: Launch(kind.definition, selected),
            harness=Harness(
                registry=kind.registry(),
                facts=FACTS,
                limits=limits_for(kind.budget(selected), _Bounds()),
                envelope_type=RunEnvelope[FlowState],
                requirements=kind.requirements(TERMS),
                schemas=kind.definition.reply_schemas,
            ),
            schemas=kind.definition.reply_schemas,
        )
        self._executors = SerialExecutors(search=search, throughputs=throughputs)

    def headline(self, commit: str) -> float | None:
        """What the candidate of ``commit`` measures (a pure function of its commit)."""
        return self._executors.headline(commit)

    def __call__(self, request: Request, core: CoreState) -> Answer:
        """What the executors report for ``request``."""
        return self._executors(request, core)
