"""Deterministic crash/resume tests for evolve's per-generation state machine.

Regression coverage for review bug R2 (old ``EvolveResumeError``,
``restore_uncommitted_evolve_state``, and the ``GenerationJournal``/
``GenerationCursor`` cursor validation it depended on were replaced by a
single durable value, ``EvolveState``, plus the deterministic
``PopulationSearch`` state machine).

Two layers:

- Pure layer (fast, hypothesis-friendly): models exactly what
  ``EvolveRun.run_generation``/``_admit_slot`` do -- derive every child
  slot's proposal from a fixed ``generation_start`` snapshot, admit slots
  sequentially, and (on "resume") only evaluate slots past
  ``admitted_slots`` -- using ``PopulationSearch`` directly, no
  ``RunContext``, agents, or filesystem. This is where the crash-point /
  pass-fail fuzzing lives, so it stays cheap.
- Wiring layer: a handful of concrete end-to-end runs (real git-tracked
  workspace, scripted ``FakeAgentClient``, same harness
  ``test_evolutionary_loop.py`` uses) proving ``EvolveRun`` itself applies
  that pure model correctly at each of the boundaries the design calls out:
  after proposals are derived, mid-parallel evaluation with some children
  done, after admit before commit, and after commit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.loops.evolve._support import (
    _default_profiler_responses,
    _invoke_loop,
    _judge_response,
    _load_population,
    _mutator_writes_callback,
    _project_dir,
    ref_file,  # noqa: F401  # tracked: #288  # pytest fixture
)

from vibesys.loops.evolve.run import EvolveRun
from vibesys.search.population.models import CandidateOutcome, PopulationConfig, Proposal
from vibesys.search.population.search import PopulationSearch
from vs_agent.api.testing import FakeAgentClient

if TYPE_CHECKING:
    from vibesys.search.population.models import PopulationState

# ---------------------------------------------------------------------------
# Pure layer: model the exact slot bookkeeping EvolveRun implements.
# ---------------------------------------------------------------------------


def _run_generation_uninterrupted(
    search: PopulationSearch, generation_start: PopulationState, outcomes: list[CandidateOutcome]
) -> PopulationState:
    """One generation, start to finish, with no crash."""
    state = generation_start
    proposals = _derive_proposals(search, generation_start, len(outcomes))
    for proposal, outcome in zip(proposals, outcomes, strict=True):
        if proposal is None or outcome is None:
            continue
        _individual, state = search.admit(state, outcome)
    return search.end_generation(state)


def _derive_proposals(
    search: PopulationSearch, generation_start: PopulationState, count: int
) -> list[Proposal | None]:
    state = generation_start
    proposals: list[Proposal | None] = []
    for _ in range(count):
        proposal, state = search.propose(state)
        proposals.append(proposal)
    return proposals


def _run_generation_with_crash(
    search: PopulationSearch,
    generation_start: PopulationState,
    outcomes: list[CandidateOutcome],
    crash_at: int,
) -> tuple[PopulationState, int]:
    """Admit only the first *crash_at* slots, exactly like a killed process.

    Returns the state a resumed process would find on disk: never further
    along than a real crash could leave it, and never missing an admitted
    slot either.
    """
    state = generation_start
    proposals = _derive_proposals(search, generation_start, len(outcomes))
    for slot, (proposal, outcome) in enumerate(zip(proposals, outcomes, strict=True), start=1):
        if slot > crash_at:
            break
        if proposal is None or outcome is None:
            continue
        _individual, state = search.admit(state, outcome)
    return state, crash_at


def _resume_generation(
    search: PopulationSearch,
    generation_start: PopulationState,
    outcomes: list[CandidateOutcome],
    partial_state: PopulationState,
    admitted_slots: int,
) -> tuple[PopulationState, list[int]]:
    """Recompute proposals identically and evaluate only the remaining slots.

    Mirrors ``EvolveRun.run_generation``: proposals are re-derived from the
    same ``generation_start`` (never from ``partial_state``), and only slots
    past ``admitted_slots`` are (re-)evaluated.
    """
    state = partial_state
    proposals = _derive_proposals(search, generation_start, len(outcomes))
    evaluated_slots: list[int] = []
    for slot, (proposal, outcome) in enumerate(zip(proposals, outcomes, strict=True), start=1):
        if slot <= admitted_slots:
            continue
        evaluated_slots.append(slot)
        if proposal is None or outcome is None:
            continue
        _individual, state = search.admit(state, outcome)
    return search.end_generation(state), evaluated_slots


def _outcome(*, passed: bool, seq: int) -> CandidateOutcome:
    return CandidateOutcome(
        passed=passed,
        parent_id=None,
        commit=f"c{seq}" if passed else None,
        perf_metric=float(seq) if passed else None,
        summary=f"s{seq}",
        feedback="" if passed else f"f{seq}",
    )


def test_resumed_generation_matches_uninterrupted_generation() -> None:
    """A crash after 2 of 4 slots, then a resume, ends identically to a run
    that never crashed."""
    search = PopulationSearch(PopulationConfig(seed=3))
    seed_state = search.initial()
    _seed, seed_state = search.admit(seed_state, _outcome(passed=True, seq=0))

    outcomes = [
        _outcome(passed=True, seq=1),
        _outcome(passed=False, seq=2),
        _outcome(passed=True, seq=3),
        _outcome(passed=True, seq=4),
    ]

    control = _run_generation_uninterrupted(search, seed_state, outcomes)

    partial_state, admitted = _run_generation_with_crash(search, seed_state, outcomes, crash_at=2)
    resumed, evaluated = _resume_generation(search, seed_state, outcomes, partial_state, admitted)

    assert resumed == control
    # Only the slots past the crash point were (re-)evaluated on resume.
    assert evaluated == [3, 4]


def test_already_admitted_slots_are_never_reevaluated() -> None:
    search = PopulationSearch(PopulationConfig(seed=5))
    seed_state = search.initial()
    _seed, seed_state = search.admit(seed_state, _outcome(passed=True, seq=0))
    outcomes = [_outcome(passed=True, seq=i) for i in range(1, 6)]

    for crash_at in range(len(outcomes) + 1):
        partial_state, admitted = _run_generation_with_crash(search, seed_state, outcomes, crash_at)
        _resumed, evaluated = _resume_generation(
            search, seed_state, outcomes, partial_state, admitted
        )
        # Every slot is accounted for exactly once across "before crash" + "resume".
        assert list(range(1, admitted + 1)) + evaluated == list(range(1, len(outcomes) + 1))


@given(
    crash_at=st.integers(min_value=0, max_value=5),
    passes=st.lists(st.booleans(), min_size=5, max_size=5),
    seed=st.integers(min_value=0, max_value=1000),
)
@settings(max_examples=200, deadline=None)
def test_crash_resume_property_matches_uninterrupted_for_any_crash_point(
    crash_at: int, passes: list[bool], seed: int
) -> None:
    """The core R2 property, fuzzed: whatever slot a process dies on, and
    whatever the scripted per-slot outcomes are, resuming from committed
    state alone reaches the exact same final population an uninterrupted
    run would, and never redoes an admitted slot."""
    search = PopulationSearch(PopulationConfig(seed=seed))
    seed_state = search.initial()
    _seed, seed_state = search.admit(seed_state, _outcome(passed=True, seq=0))
    outcomes = [_outcome(passed=passed, seq=i) for i, passed in enumerate(passes, start=1)]

    control = _run_generation_uninterrupted(search, seed_state, outcomes)

    partial_state, admitted = _run_generation_with_crash(search, seed_state, outcomes, crash_at)
    resumed, evaluated = _resume_generation(search, seed_state, outcomes, partial_state, admitted)

    assert resumed == control
    assert evaluated == list(range(admitted + 1, len(outcomes) + 1))


# ---------------------------------------------------------------------------
# Wiring layer: EvolveRun applies the pure model at each real boundary.
# ---------------------------------------------------------------------------


class _SimulatedCrashError(Exception):
    """Raised in place of a real process kill, at a chosen point."""


def _crash_after(target: type, method_name: str, n: int) -> tuple[Any, dict[str, int]]:
    """Patch *method_name* to run normally *n* times, then raise.

    Returns a ``(patcher, calls)`` pair; ``calls["count"]`` is the number of
    times the real method actually ran before the crash.
    """
    original = getattr(target, method_name)
    calls = {"count": 0}

    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if calls["count"] >= n:
            raise _SimulatedCrashError
        calls["count"] += 1
        return await original(self, *args, **kwargs)

    return patch.object(target, method_name, wrapper), calls


class _ResumeKwargs(TypedDict):
    """The exact keys ``_resume_kwargs`` supplies.

    Narrower than ``_EvolveLoopKwargs`` (whose every field is optional) so
    that splatting this alongside other explicit ``_invoke_loop`` keyword
    arguments type-checks without ty seeing a possible key collision.
    """

    input_path: str
    exp_name: str
    existing: bool


def _resume_kwargs(tmp_path) -> _ResumeKwargs:  # noqa: ANN001
    """The exp_name/input_path a resumed ``_invoke_loop`` call needs.

    Mirrors ``test_evolve_with_preexisting_passing_seed_skips_bootstrap``:
    the generated run directory (not the reference-file directory) must be
    reused as the project root, keyed by its own generated exp name.
    """
    project = _project_dir(tmp_path)
    return {"input_path": str(project), "exp_name": project.name, "existing": True}


def _script(n_children: int, *, all_pass: bool = True) -> FakeAgentClient:
    runner = FakeAgentClient()
    runner.on_invoke(_mutator_writes_callback(runner))
    # One bootstrap attempt + n_children generation-1 candidates.
    verdicts = [_judge_response("pass")] * (1 + n_children) if all_pass else None
    if verdicts is not None:
        runner.enqueue("judge", *verdicts)
    runner.enqueue("profiler", *_default_profiler_responses(1 + n_children))
    return runner


def test_crash_before_any_generation_evaluation_resumes_cleanly(tmp_path, ref_file) -> None:  # noqa: ANN001, F811
    """Boundary: crash right after proposals are derived, before any child
    is evaluated. Resume evaluates every child slot exactly once."""
    n = 3
    runner = _script(n)
    patcher, calls = _crash_after(EvolveRun, "_evaluate_serial", n=0)
    with patcher, pytest.raises(_SimulatedCrashError):
        _invoke_loop(tmp_path, ref_file, runner, max_generations=1, children_per_generation=n)
    assert calls["count"] == 0

    result = _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        max_generations=1,
        children_per_generation=n,
        **_resume_kwargs(tmp_path),
    )
    assert result is True
    pop = _load_population(tmp_path)
    assert len(pop) == n + 1  # seed + every child, evaluated exactly once total
    assert len(runner.calls_for("implementer")) == 1 + n


def test_crash_after_one_admit_does_not_redo_it(tmp_path, ref_file) -> None:  # noqa: ANN001, F811
    """Boundary: crash after one child is admitted and committed. Resume
    picks up at the next slot; the admitted child is not re-evaluated."""
    n = 3
    runner = _script(n)
    patcher, calls = _crash_after(EvolveRun, "_evaluate_serial", n=1)
    with patcher, pytest.raises(_SimulatedCrashError):
        _invoke_loop(tmp_path, ref_file, runner, max_generations=1, children_per_generation=n)
    assert calls["count"] == 1

    result = _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        max_generations=1,
        children_per_generation=n,
        **_resume_kwargs(tmp_path),
    )
    assert result is True
    pop = _load_population(tmp_path)
    assert len(pop) == n + 1
    # Exactly n children were ever evaluated in total (1 before the crash +
    # n - 1 after resume), never n + 1.
    assert len(runner.calls_for("implementer")) == 1 + n


def test_crash_after_admit_before_commit_redoes_only_that_slot(tmp_path, ref_file) -> None:  # noqa: ANN001, F811
    """Boundary: the in-memory admit happened, but the checkpoint never
    reached disk. A resumed process cannot see it, so it evaluates that slot
    again -- and produces the same final population as an uninterrupted
    run, since evaluation here is deterministic (scripted)."""
    n = 2
    runner = _script(n)
    patcher, calls = _crash_after(EvolveRun, "persist", n=1)  # begin-generation's persist succeeds
    with patcher, pytest.raises(_SimulatedCrashError):
        _invoke_loop(tmp_path, ref_file, runner, max_generations=1, children_per_generation=n)
    assert calls["count"] == 1  # begin-generation's persist committed; child 1's did not

    result = _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        max_generations=1,
        children_per_generation=n,
        **_resume_kwargs(tmp_path),
    )
    assert result is True
    pop = _load_population(tmp_path)
    assert len(pop) == n + 1
    # The first child's evaluation was redone once (its commit never landed),
    # so total implementer calls are one more than the crash-free count.
    assert len(runner.calls_for("implementer")) == 1 + n + 1


def test_crash_mid_parallel_batch_admits_nothing_from_it(tmp_path, ref_file) -> None:  # noqa: ANN001, F811
    """Boundary: the parallel pool dies partway through. Since outcomes are
    only admitted after the whole pool returns, nothing from that batch is
    committed, and a resume re-evaluates the entire batch (never a partial,
    inconsistent admission)."""

    async def _dying_pool(self: Any, generation: int, targets: Any) -> Any:  # noqa: ARG001, ANN401
        raise _SimulatedCrashError

    n = 2
    runner = _script(n)
    with (
        patch.object(EvolveRun, "parallel", property(lambda self: True)),  # noqa: ARG005
        patch.object(EvolveRun, "_evaluate_parallel_pool", _dying_pool),
        pytest.raises(_SimulatedCrashError),
    ):
        _invoke_loop(
            tmp_path,
            ref_file,
            runner,
            max_generations=1,
            children_per_generation=n,
            max_parallelism=n,
        )
    # Nothing from generation 1 was admitted.
    pop = _load_population(tmp_path)
    assert len(pop) == 1  # bootstrap seed only

    result = _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        max_generations=1,
        children_per_generation=n,
        max_parallelism=n,
        **_resume_kwargs(tmp_path),
    )
    assert result is True
    pop = _load_population(tmp_path)
    assert len(pop) == n + 1
