"""``ctx.state.commit`` derives round/experiment events strategy-agnostically.

Covers the run integration mechanism: after a
checkpoint, the run diffs the previous published `RunView` against the new
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
from vibesys.run.host import open_product_run_host

from vibesys.api import ComputeBackend, Config, OrchestrationRegistry, create_session
from vibesys.events import CoreEvent, CoreEventType, ExperimentsChangedData
from vibesys.inputs import load_input_bundle
from vibesys.orchestration.profilers import ProfilerKind
from vibesys.run.contracts import ResumeRef, RoundSummary, RunRequest, RunStatus, RunView
from vibesys.run.integration import LocalRunIntegration
from vs_project.api import OrchestrationDescriptor
from vs_runtime.api import OrchestrationPlugin, PluginProjection, ProjectedRound, Run
from vs_runtime.api import RunStatus as PluginRunStatus

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class _FakeAgentState(BaseModel):
    """Minimal typed state carrying only what the projector needs."""

    round_numbers: tuple[int, ...] = ()
    experiment_revision: int = 0
    profile_skipped: bool = False


class _Options(BaseModel):
    pass


async def _orchestrate(run: Run, options: BaseModel) -> PluginRunStatus:
    _Options.model_validate(options)
    previous = await run.state.load(_FakeAgentState)
    round_numbers = previous.round_numbers if previous is not None else ()
    revision = previous.experiment_revision if previous is not None else 0
    await run.state.commit(
        _FakeAgentState(
            round_numbers=(*round_numbers, len(round_numbers) + 1),
            experiment_revision=revision + 1,
        )
    )
    return PluginRunStatus.SUCCEEDED


def _project_state(state: BaseModel) -> PluginProjection:
    typed = _FakeAgentState.model_validate(state)
    return PluginProjection(
        payload=None,
        rounds=tuple(
            ProjectedRound(
                number=number,
                status="completed",
                attempts=1,
                judge_verdict="pass",
                profile_skipped=typed.profile_skipped,
            )
            for number in typed.round_numbers
        ),
        experiment_revision=typed.experiment_revision,
    )


_PLUGIN = OrchestrationPlugin(
    id="commit-probe",
    agents=(),
    options=_Options,
    orchestrate=_orchestrate,
    state=_FakeAgentState,
    project=_project_state,
)


class _FakeProjector:
    """Project `_FakeAgentState` into the generic `"kind": "agent"` shape."""

    def view(self, project: object, run_id: str, *, status: RunStatus, loop: str) -> RunView:
        raise NotImplementedError

    def project_committed(self, namespace: str, state: BaseModel, *, run_id: str) -> RunView | None:
        if namespace != "commit-probe" or not isinstance(state, _FakeAgentState):
            return None
        return _agent_view(
            state.round_numbers,
            state.experiment_revision,
            run_id=run_id,
            profile_skipped=state.profile_skipped,
        )


def _write_project(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "OBJECTIVE.md").write_text("Improve the queue.\n")
    (root / "queue.py").write_text("VALUE = 1\n")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n'
    )


def _discard_event(event: object) -> None:
    del event


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


def test_commit_derives_round_finished_and_experiments_changed(tmp_path: Path) -> None:
    """One session: round completion and a bare revision bump each fire once."""
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration()
    captured: list[tuple[CoreEventType, object]] = []
    integration.events.subscribe(lambda event: captured.append((event.type, event.data)))

    async def exercise() -> None:
        async with open_product_run_host(
            _request(project_root),
            integration,
            projector=_FakeProjector(),
            plugin=_PLUGIN,
        ) as ctx:
            # First commit ever for this run: establishes round 1, revision 1.
            # No prior view exists, so no EXPERIMENTS_CHANGED fires for it
            # (nothing was previously observed to compare against).
            await ctx.state.commit(_FakeAgentState(round_numbers=(1,), experiment_revision=1))
            # Bare revision bump, no new round: EXPERIMENTS_CHANGED only.
            await ctx.state.commit(_FakeAgentState(round_numbers=(1,), experiment_revision=2))
            # New round 2, same revision as before this commit -> revision 3:
            # both ROUND_FINISHED and EXPERIMENTS_CHANGED fire.
            await ctx.state.commit(_FakeAgentState(round_numbers=(1, 2), experiment_revision=3))
            # Re-committing identical content: nothing new is observable, so
            # no derived events fire at all (no double emission).
            await ctx.state.commit(_FakeAgentState(round_numbers=(1, 2), experiment_revision=3))

    try:
        asyncio.run(exercise())
    finally:
        integration.close()

    round_finished = [data for kind, data in captured if kind is CoreEventType.ROUND_FINISHED]
    # "project_attached" is unrelated: product resource composition emits it once when the
    # project attaches, independent of this run mechanism.
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
    """A fresh public session on a resumed run only emits events for new work."""
    project_root = tmp_path / "project"
    _write_project(project_root)
    registry = OrchestrationRegistry()
    registry.register_plugin(_PLUGIN)

    async def commit_round_one() -> str:
        session = create_session(_request(project_root), sink=_discard_event, registry=registry)
        session.start()
        try:
            result = await session.await_result()
            return result.run_id
        finally:
            session.close()

    run_id = asyncio.run(commit_round_one())

    captured: list[tuple[CoreEventType, object]] = []

    async def resume_and_commit_round_two() -> None:
        session = create_session(
            _request(project_root, resume=ResumeRef(run_id=run_id)),
            sink=lambda event: captured.append((event.type, event.data)),
            registry=registry,
        )
        session.start()
        try:
            result = await session.await_result()
            assert result.succeeded
        finally:
            session.close()

    asyncio.run(resume_and_commit_round_two())

    round_finished_events = [d for k, d in captured if k is CoreEventType.ROUND_FINISHED]
    assert len(round_finished_events) == 1  # only round 2 -- round 1 is not replayed


def _agent_view(
    round_numbers: Sequence[int],
    revision: int,
    *,
    run_id: str = "probe",
    profile_skipped: bool = False,
) -> RunView:
    return RunView(
        run_id=run_id,
        loop="commit-probe",
        status=RunStatus.ACTIVE,
        rounds=tuple(
            RoundSummary(
                number=number,
                status="completed",
                attempts=1,
                judge_verdict="pass",
                profile_skipped=profile_skipped,
            )
            for number in round_numbers
        ),
        experiment_revision=revision,
    )


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
    process restart partway through: the "previous view" the run diffs
    against is recomputed from the last-committed state (exactly what
    ``Run.state`` does when it re-reads durable state after a
    process restart), which must be indistinguishable from an in-memory
    cache for the invariants below to hold.
    """
    round_numbers: list[int] = []
    revision = 0
    states: list[_FakeAgentState] = [_FakeAgentState()]
    for new_rounds, revision_delta in steps:
        next_round = (round_numbers[-1] + 1) if round_numbers else 1
        round_numbers.extend(range(next_round, next_round + new_rounds))
        revision += revision_delta + (1 if new_rounds else 0)
        states.append(
            _FakeAgentState(
                round_numbers=tuple(round_numbers),
                experiment_revision=revision,
            )
        )

    integration = LocalRunIntegration()
    observer = integration.state_commit_observer("probe", _FakeProjector(), "commit-probe")
    captured = []
    integration.events.subscribe(captured.append)
    seen_round_finished: list[int] = []
    for index in range(1, len(states)):
        before = states[index - 1]
        # "Resume": recompute `before` from the durable value rather than
        # reusing the Python object from the prior loop iteration. Both must
        # produce identical diff results.
        if index == resume_at:
            before = _FakeAgentState.model_validate(before.model_dump())
        observer.committed(before, states[index])

    for event in captured:
        if event.type is CoreEventType.ROUND_FINISHED:
            assert event.round_label is not None
            number = int(event.round_label.removeprefix("round-"))
            assert number not in seen_round_finished, "round finished twice"
            seen_round_finished.append(number)

    assert sorted(seen_round_finished) == round_numbers

    # EXPERIMENTS_CHANGED fired exactly when the revision differed between
    # consecutive views (skipping the very first commit, which has nothing
    # to compare against).
    changed_count = sum(1 for event in captured if event.type is CoreEventType.EXPERIMENTS_CHANGED)
    expected_changes = sum(
        1
        for index in range(1, len(states))
        if states[index - 1].experiment_revision != states[index].experiment_revision
    )
    assert changed_count == expected_changes


def test_commit_projection_preserves_profile_skip_and_publication_order() -> None:
    integration = LocalRunIntegration()
    order: list[str] = []
    captured = []

    def capture_event(event: CoreEvent) -> None:
        order.append(event.type.value)
        captured.append(event)

    integration.add_committed_state_listener(
        lambda _namespace, _state, _changed: order.append("state_published")
    )
    integration.events.subscribe(capture_event)
    observer = integration.state_commit_observer("probe", _FakeProjector(), "commit-probe")

    observer.committed(
        None,
        _FakeAgentState(
            round_numbers=(1,),
            experiment_revision=1,
            profile_skipped=True,
        ),
    )

    round_finished = next(event for event in captured if event.type is CoreEventType.ROUND_FINISHED)
    assert round_finished.data is not None
    assert round_finished.data.kind == "round_finished"
    assert round_finished.data.profile_skipped is True
    assert order[:2] == ["state_published", CoreEventType.ROUND_FINISHED.value]
