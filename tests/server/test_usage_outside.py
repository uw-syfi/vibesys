"""A transcript session is a person's own unless a run of the host made it.

The three layers (the run's journaled session ids, the place only a run makes, and a guess for
older runs) are checked from the contract: rows are the transcript cache's, runs are what their
rollups say, and the working directories are drawn from the layout the project library
documents, including ones inside a run's worktrees.
"""

from __future__ import annotations

from datetime import UTC, datetime

from hypothesis import example, given
from hypothesis import strategies as st

from server.usage_outside import ProviderRow, RunFacts, exclude
from server.usage_rollup import QUARTER_SECONDS
from vs_project.api import Project
from vs_proto.api import UsageSpeed, usage_state

BASE = 1_790_000_000 // QUARTER_SECONDS
ROOT = "/work/alpha"
RUN_ID = "20261010-120000-11111111-dynamic"
INSIDE = f"/work/alpha/.vibesys/state/local/runs/{RUN_ID}/worktrees/cand-1/src"
PROVIDERS = ("claude", "codex")


def _row(session: str, cwd: str, quarter: int, turns: int = 1) -> usage_state.TranscriptRow:
    return usage_state.TranscriptRow(
        quarter=quarter,
        session_id=session,
        cwd=cwd,
        model="m",
        speed=UsageSpeed.STANDARD,
        input=10,
        output=1,
        turns=turns,
    )


_CWDS = st.sampled_from(
    [ROOT, "/work/beta", INSIDE, "", "/elsewhere/.vibesys/state/local/runs/x y/worktrees/a"]
)
_ROWS = st.builds(
    ProviderRow,
    st.sampled_from(PROVIDERS),
    st.builds(
        _row,
        st.sampled_from(["s1", "s2", "s3", "s4"]),
        _CWDS,
        st.integers(BASE, BASE + 40),
        st.integers(1, 5),
    ),
)


def _facts(
    ids: frozenset[str],
    *,
    complete: bool,
    providers: frozenset[str],
    first: int = 0,
    last: int = 40,
) -> RunFacts:
    return RunFacts(
        run_id=RUN_ID,
        project_root=ROOT,
        providers=providers,
        session_ids=ids,
        session_ids_complete=complete,
        first_turn_at=datetime.fromtimestamp((BASE + first) * QUARTER_SECONDS, UTC),
        last_turn_at=datetime.fromtimestamp((BASE + last) * QUARTER_SECONDS, UTC),
    )


_RUNS = st.lists(
    st.builds(
        _facts,
        st.frozensets(st.sampled_from(["s1", "s2", "s3", "s4"]), max_size=3),
        complete=st.booleans(),
        providers=st.frozensets(st.sampled_from(PROVIDERS), min_size=1),
        first=st.integers(0, 40),
        last=st.integers(0, 40),
    ),
    max_size=3,
)


@given(rows=st.lists(_ROWS, max_size=12), runs=_RUNS)
def test_a_session_a_run_named_or_a_place_only_a_run_makes_is_never_a_persons(
    rows: list[ProviderRow], runs: list[RunFacts]
) -> None:
    left = exclude(rows, runs, Project.run_id_for_workspace)
    named = {identifier for run in runs for identifier in run.session_ids}
    for kept in left.kept:
        assert kept.row.session_id not in named
        assert Project.run_id_for_workspace(kept.row.cwd) is None


@given(rows=st.lists(_ROWS, max_size=12), runs=_RUNS)
def test_a_session_no_run_claims_is_never_excluded_by_the_exact_layers(
    rows: list[ProviderRow], runs: list[RunFacts]
) -> None:
    """With every run complete, the kept rows are exactly the rows no id and no place claims."""
    complete = [_facts(run.session_ids, complete=True, providers=run.providers) for run in runs]
    named = {identifier for run in complete for identifier in run.session_ids}
    left = exclude(rows, complete, Project.run_id_for_workspace)
    expected = [
        found
        for found in rows
        if found.row.session_id not in named and Project.run_id_for_workspace(found.row.cwd) is None
    ]
    assert list(left.kept) == expected
    assert left.excluded_turns == {}


@example(
    rows=[ProviderRow("claude", _row("s9", ROOT, BASE + 5, turns=3))],
    runs=[_facts(frozenset(), complete=False, providers=frozenset({"claude"}))],
)
@given(rows=st.lists(_ROWS, max_size=12), runs=_RUNS)
def test_the_guess_acts_only_for_runs_that_did_not_name_every_session(
    rows: list[ProviderRow], runs: list[RunFacts]
) -> None:
    """Removing the guess's runs (those that named all) from the facts changes nothing for them."""
    named_all = [run for run in runs if run.session_ids_complete]
    only = exclude(rows, named_all, Project.run_id_for_workspace)
    assert only.excluded_turns == {}
    everyone = exclude(rows, runs, Project.run_id_for_workspace)
    assert set(map(id, only.kept)) >= set(map(id, everyone.kept))


def test_a_turn_of_the_same_provider_at_the_projects_root_inside_the_runs_time_is_guessed() -> None:
    run = _facts(frozenset(), complete=False, providers=frozenset({"claude"}), first=10, last=20)
    inside = ProviderRow("claude", _row("p1", ROOT, BASE + 15, turns=2))
    other_provider = ProviderRow("codex", _row("p2", ROOT, BASE + 15))
    other_place = ProviderRow("claude", _row("p3", "/work/beta", BASE + 15))
    before = ProviderRow("claude", _row("p4", ROOT, BASE + 5))
    after = ProviderRow("claude", _row("p5", ROOT, BASE + 30))
    left = exclude(
        [inside, other_provider, other_place, before, after], [run], Project.run_id_for_workspace
    )
    assert list(left.kept) == [other_provider, other_place, before, after]
    assert left.excluded_turns == {"claude": 2}


def test_exact_exclusions_are_counted_by_session_and_only_within_the_counted_range() -> None:
    run = _facts(frozenset({"s1"}), complete=True, providers=frozenset({"claude"}))
    rows = [
        ProviderRow("claude", _row("s1", ROOT, BASE + 1)),
        ProviderRow("claude", _row("s1", ROOT, BASE + 2)),
        ProviderRow("claude", _row("s2", INSIDE, BASE + 30)),
        ProviderRow("claude", _row("s3", ROOT, BASE + 3)),
    ]
    window = (BASE * QUARTER_SECONDS, (BASE + 10) * QUARTER_SECONDS)
    left = exclude(rows, [run], Project.run_id_for_workspace, counted=window)
    assert left.excluded_sessions == {"claude": frozenset({"s1"})}
    assert [found.row.session_id for found in left.kept] == ["s3"]


def test_a_workspace_path_names_its_run_by_the_path_alone() -> None:
    assert Project.run_id_for_workspace(INSIDE) == RUN_ID
    assert Project.run_id_for_workspace(f"/x/.vibesys/state/local/runs/{RUN_ID}/logs") is None
    assert (
        Project.run_id_for_workspace(f"/x/.vibesys/state/local/runs/{RUN_ID}/worktrees") == RUN_ID
    )
    assert Project.run_id_for_workspace("/x/.vibesys/state/local/runs/bad id/worktrees/a") is None
    assert Project.run_id_for_workspace("") is None
