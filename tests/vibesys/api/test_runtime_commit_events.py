"""``ctx.state.commit`` derives round/experiment events strategy-agnostically.

Covers the host mechanism in ``vibesys.orchestration.state``: after a
checkpoint, the host diffs the previous published `RunView` against the new
one and emits `ROUND_FINISHED` for newly observed rounds and
`EXPERIMENTS_CHANGED` when the experiment revision moved. These tests never
reference a specific strategy ID; they use a synthetic ``"kind": "agent"``
projection, the same discriminator the real read models use (see
``vibesys.agent_run.readmodel.AgentRunProjection``).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.context import RunSetup
from vibesys.evaluators.input_manifest import load_input_bundle
from vibesys.events import CoreEventType, ExperimentsChangedData
from vibesys.orchestration.request import ResumeRef, RunRequest
from vibesys.orchestration.runtime import RunContext
from vibesys.orchestration.state import _emit_commit_events
from vibesys.orchestration.view import RoundSummary, RunStatus, RunView
from vibesys.profilers import ProfilerKind
from vibesys.run.integration import LocalRunIntegration
from vs_project.api import OrchestrationDescriptor

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class _FakeAgentState(BaseModel):
    """Minimal typed state carrying only what the projector needs."""

    round_numbers: tuple[int, ...] = ()
    experiment_revision: int = 0


class _FakeProjector:
    """Project `_FakeAgentState` into the generic `"kind": "agent"` shape."""

    def view(self, project: object, run_id: str, *, status: RunStatus, loop: str) -> RunView:
        raise NotImplementedError

    def project_committed(self, namespace: str, state: BaseModel, *, run_id: str) -> RunView | None:
        if namespace != "commit_probe" or not isinstance(state, _FakeAgentState):
            return None
        return _agent_view(state.round_numbers, state.experiment_revision, run_id=run_id)


def _write_project(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "OBJECTIVE.md").write_text("Improve the queue.\n")
    (root / "queue.py").write_text("VALUE = 1\n")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n'
    )


def _request(
    project_root: Path, *, exp_name: str = "commit-probe", resume: ResumeRef | None = None
) -> RunRequest:
    return RunRequest(
        project_root=project_root,
        orchestration=OrchestrationDescriptor(id="commit-probe", config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "commit-probe"}}),
        input_bundle=load_input_bundle(project_root),
        objective="Improve the queue.",
        exp_name=exp_name,
        resume=resume,
        agent_backend="stub",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )


def _setup() -> RunSetup:
    return RunSetup(state_namespace="commit_probe", state_slots={"state.json": _FakeAgentState})


def test_commit_derives_round_finished_and_experiments_changed(tmp_path: Path) -> None:
    """One session: round completion and a bare revision bump each fire once."""
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration()
    captured: list[tuple[CoreEventType, object]] = []
    integration.events.subscribe(lambda event: captured.append((event.type, event.data)))

    async def exercise() -> None:
        async with RunContext.open(
            _request(project_root), integration, setup=_setup(), projector=_FakeProjector()
        ) as ctx:
            # First commit ever for this run: establishes round 1, revision 1.
            # No prior view exists, so no EXPERIMENTS_CHANGED fires for it
            # (nothing was previously observed to compare against).
            await ctx.state.commit(
                sequence=1,
                writes={"state.json": _FakeAgentState(round_numbers=(1,), experiment_revision=1)},
                candidate=False,
            )
            # Bare revision bump, no new round: EXPERIMENTS_CHANGED only.
            await ctx.state.commit(
                sequence=2,
                writes={"state.json": _FakeAgentState(round_numbers=(1,), experiment_revision=2)},
                candidate=False,
            )
            # New round 2, same revision as before this commit -> revision 3:
            # both ROUND_FINISHED and EXPERIMENTS_CHANGED fire.
            await ctx.state.commit(
                sequence=3,
                writes={"state.json": _FakeAgentState(round_numbers=(1, 2), experiment_revision=3)},
                candidate=False,
            )
            # Re-committing identical content: nothing new is observable, so
            # no derived events fire at all (no double emission).
            await ctx.state.commit(
                sequence=4,
                writes={"state.json": _FakeAgentState(round_numbers=(1, 2), experiment_revision=3)},
                candidate=False,
            )

    try:
        asyncio.run(exercise())
    finally:
        integration.close()

    round_finished = [data for kind, data in captured if kind is CoreEventType.ROUND_FINISHED]
    # "project_attached" is unrelated: `vibesys.context` emits it once when the
    # project attaches, independent of this host mechanism.
    experiments_changed = [
        data
        for kind, data in captured
        if kind is CoreEventType.EXPERIMENTS_CHANGED
        and isinstance(data, ExperimentsChangedData)
        and data.reason != "project_attached"
    ]
    assert len(round_finished) == 2  # round 1, round 2 -- each exactly once
    assert len(experiments_changed) == 2  # revision 1->2, then 2->3
    assert experiments_changed[0].reason == "active_hypothesis_changed"
    assert experiments_changed[1].reason == "round_persisted"


def test_resume_does_not_replay_already_committed_rounds(tmp_path: Path) -> None:
    """A fresh `RunContext` on a resumed run only emits events for new work."""
    project_root = tmp_path / "project"
    _write_project(project_root)

    async def commit_round_one() -> str:
        integration = LocalRunIntegration()
        try:
            async with RunContext.open(
                _request(project_root), integration, setup=_setup(), projector=_FakeProjector()
            ) as ctx:
                await ctx.state.commit(
                    sequence=1,
                    writes={
                        "state.json": _FakeAgentState(round_numbers=(1,), experiment_revision=1)
                    },
                    candidate=False,
                )
                return ctx._resources.run_id  # noqa: SLF001  # LW-040114 [SLF001]; this test reads one private attribute to check internal wiring that has no public accessor.
        finally:
            integration.close()

    run_id = asyncio.run(commit_round_one())

    resumed_integration = LocalRunIntegration()
    captured: list[tuple[CoreEventType, object]] = []
    resumed_integration.events.subscribe(lambda event: captured.append((event.type, event.data)))

    async def resume_and_commit_round_two() -> None:
        async with RunContext.open(
            _request(project_root, resume=ResumeRef(run_id=run_id)),
            resumed_integration,
            setup=_setup(),
            projector=_FakeProjector(),
        ) as ctx:
            await ctx.state.commit(
                sequence=2,
                writes={"state.json": _FakeAgentState(round_numbers=(1, 2), experiment_revision=2)},
                candidate=False,
            )

    try:
        asyncio.run(resume_and_commit_round_two())
    finally:
        resumed_integration.close()

    round_finished_events = [d for k, d in captured if k is CoreEventType.ROUND_FINISHED]
    assert len(round_finished_events) == 1  # only round 2 -- round 1 is not replayed


def _agent_view(round_numbers: Sequence[int], revision: int, *, run_id: str = "probe") -> RunView:
    return RunView(
        run_id=run_id,
        loop="commit-probe",
        status=RunStatus.ACTIVE,
        rounds=tuple(
            RoundSummary(number=number, status="completed", attempts=1, judge_verdict="pass")
            for number in round_numbers
        ),
        experiment_revision=revision,
    )


class _FakeEvents:
    def __init__(self) -> None:
        self.calls: list[tuple[CoreEventType, dict[str, object]]] = []

    def emit(self, event_type: CoreEventType, *args: object, **kwargs: object) -> None:
        del args
        self.calls.append((event_type, kwargs))


@given(
    steps=st.lists(
        st.tuples(st.integers(min_value=0, max_value=3), st.integers(min_value=0, max_value=2)),
        min_size=0,
        max_size=12,
    ),
    resume_at=st.integers(min_value=0, max_value=12),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_commit_events_property_no_double_emission_and_revision_iff_changed(
    steps: list[tuple[int, int]], resume_at: int
) -> None:
    """Across any sequence of commits, each round fires once and revision-iff.

    ``steps`` is a list of ``(new_rounds, revision_delta)`` pairs describing
    each commit's effect on the durable state. ``resume_at`` simulates a
    process restart partway through: the "previous view" the host diffs
    against is recomputed from the last-committed state (exactly what
    ``RunContext.state`` does when it re-reads durable state after a
    process restart), which must be indistinguishable from an in-memory
    cache for the invariants below to hold.
    """
    round_numbers: list[int] = []
    revision = 0
    views: list[RunView] = [_agent_view((), 0)]
    for new_rounds, revision_delta in steps:
        next_round = (round_numbers[-1] + 1) if round_numbers else 1
        round_numbers.extend(range(next_round, next_round + new_rounds))
        revision += revision_delta + (1 if new_rounds else 0)
        views.append(_agent_view(tuple(round_numbers), revision))

    events = _FakeEvents()
    seen_round_finished: list[int] = []
    for index in range(1, len(views)):
        before = views[index - 1]
        # "Resume": recompute `before` from the durable value rather than
        # reusing the Python object from the prior loop iteration. Both must
        # produce identical diff results.
        if index == resume_at:
            before = RunView.model_validate(before.model_dump())
        _emit_commit_events(events, before, views[index])

    for event_type, kwargs in events.calls:
        if event_type is CoreEventType.ROUND_FINISHED:
            label = kwargs["round_label"]
            assert isinstance(label, str)
            number = int(label.removeprefix("round-"))
            assert number not in seen_round_finished, "round finished twice"
            seen_round_finished.append(number)

    assert sorted(seen_round_finished) == round_numbers

    # EXPERIMENTS_CHANGED fired exactly when the revision differed between
    # consecutive views (skipping the very first commit, which has nothing
    # to compare against).
    changed_count = sum(
        1 for event_type, _ in events.calls if event_type is CoreEventType.EXPERIMENTS_CHANGED
    )
    expected_changes = sum(
        1
        for index in range(1, len(views))
        if views[index - 1].experiment_revision != views[index].experiment_revision
    )
    assert changed_count == expected_changes
